# -*- coding: utf-8 -*-
# Odoo 14.0 — Hold stock directly on stock.quant.reserved_quantity (no stock.move created)
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_is_zero, float_round

from .tools import LOT_MODEL, PACKAGE_MODEL, quant_reserve, quant_unreserve, uom_rounding


class PlanReservation(models.Model):
    _name = 'plan.reservation'
    _description = 'Plan Stock Reservation (hold)'
    _order = 'plan_id, bom_level, product_id, id'

    plan_id = fields.Many2one('plan.order', required=True, ondelete='cascade', index=True)
    plan_line_id = fields.Many2one('plan.order.line', ondelete='set null', index=True,
                                   help='Only set on level 0 (finished good of the plan line).')
    company_id = fields.Many2one(related='plan_id.company_id', store=True)
    product_id = fields.Many2one('product.product', required=True, index=True)
    parent_product_id = fields.Many2one('product.product', string='Used For (parent)')
    bom_level = fields.Integer(string='BOM level', default=0)
    location_id = fields.Many2one('stock.location', required=True)
    lot_id = fields.Many2one(LOT_MODEL)
    package_id = fields.Many2one(PACKAGE_MODEL)
    owner_id = fields.Many2one('res.partner')
    quant_id = fields.Many2one('stock.quant', ondelete='set null',
                               help='Informational only; quants may be merged by the cron.')
    quantity = fields.Float(digits='Product Unit of Measure', required=True,
                            help='Quantity currently held, in the product UoM.')
    product_uom_id = fields.Many2one(related='product_id.uom_id')
    quantity_released = fields.Float(digits='Product Unit of Measure', readonly=True,
                                     help='Quantity released last time (used by Hold).')
    state = fields.Selection([
        ('reserved', 'Reserved'),
        ('released', 'Released'),
        ('consumed', 'Consumed by plan'),
    ], default='reserved', required=True, index=True)

    # ------------------------------------------------------------------
    # Low-level quant helpers (batch-friendly)
    # ------------------------------------------------------------------
    @api.model
    def _reserve_on_quants(self, product, location, qty):
        """Reserve at most `qty` (product UoM) at `location` (child_of).
        Returns [(quant, qty_reserved)]. Never raises for missing stock."""
        Quant = self.env['stock.quant']
        rounding = uom_rounding(product.uom_id)
        if float_compare(qty, 0, precision_rounding=rounding) <= 0:
            return []
        available = Quant._get_available_quantity(product, location, strict=False)
        to_reserve = float_round(min(available, qty), precision_rounding=rounding)
        if float_compare(to_reserve, 0, precision_rounding=rounding) <= 0:
            return []
        # Same core methods used by stock.move._action_assign -> Inventory shows the right Reserved qty
        return quant_reserve(self.env, product, location, to_reserve)

    @api.model
    def _prepare_vals(self, plan, product, reserved_quants, level=0, parent=None, plan_line=None):
        return [{
            'plan_id': plan.id,
            'plan_line_id': plan_line.id if plan_line else False,
            'product_id': product.id,
            'parent_product_id': parent.id if parent else False,
            'bom_level': level,
            'location_id': quant.location_id.id,
            'lot_id': quant.lot_id.id,
            'package_id': quant.package_id.id,
            'owner_id': quant.owner_id.id,
            'quant_id': quant.id,
            'quantity': q,
            'state': 'reserved',
        } for quant, q in reserved_quants if not float_is_zero(
            q, precision_rounding=uom_rounding(product.uom_id))]

    def _release(self, new_state='released'):
        """Release the hold on the quants. Looked up strictly by (product, location, lot, package,
        owner) and capped at the current reserved_quantity, so it stays safe when quants were merged
        by the cron or another move was done meanwhile."""
        Quant = self.env['stock.quant']
        for res in self.filtered(lambda r: r.state == 'reserved'):
            rounding = uom_rounding(res.product_id.uom_id)
            quants = Quant._gather(res.product_id, res.location_id, lot_id=res.lot_id,
                                   package_id=res.package_id, owner_id=res.owner_id, strict=True)
            reserved_now = sum(quants.mapped('reserved_quantity'))
            qty = float_round(min(res.quantity, reserved_now), precision_rounding=rounding)
            if float_compare(qty, 0, precision_rounding=rounding) > 0:
                quant_unreserve(self.env, res.product_id, res.location_id, qty,
                                lot=res.lot_id, package=res.package_id, owner=res.owner_id)
            res.write({'state': new_state, 'quantity_released': res.quantity})
        return True

    def _cascade_lower_levels(self):
        """After the planner changed a hold on level n -> recompute the holds of levels > n."""
        if self.env.context.get('plan_no_cascade'):
            return
        by_plan = {}
        for r in self:
            by_plan[r.plan_id] = min(by_plan.get(r.plan_id, r.bom_level), r.bom_level)
        for plan, level in by_plan.items():
            plan.with_context(plan_no_cascade=True)._reserve_lower_levels(level)

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_release(self):
        if any(r.plan_id.state not in ('confirmed',) for r in self):
            raise UserError(_('Holds can only be released while the Plan Order is Confirmed.'))
        self._release('released')
        self._cascade_lower_levels()
        return True

    def action_hold(self):
        """Hold a released line again: reserve up to the previous quantity at the original location."""
        vals_list = []
        for res in self:
            if res.state != 'released':
                continue
            if res.plan_id.state != 'confirmed':
                raise UserError(_('Holds can only be re-held while the Plan Order is Confirmed.'))
            target = res.quantity or res.quantity_released
            reserved = self._reserve_on_quants(res.product_id, res.plan_id.warehouse_id.lot_stock_id, target)
            if not reserved:
                raise UserError(_('%s: no stock available to hold again.') % res.product_id.display_name)
            vals_list += self._prepare_vals(
                res.plan_id, res.product_id, reserved, level=res.bom_level,
                parent=res.parent_product_id, plan_line=res.plan_line_id)
            res.unlink()
        if vals_list:
            self.create(vals_list)._cascade_lower_levels()
        return True

    def write(self, vals):
        """Let the planner edit Quantity directly: decrease -> release the difference, increase -> hold more."""
        if 'quantity' not in vals or self.env.context.get('plan_skip_qty_sync'):
            return super().write(vals)
        Quant = self.env['stock.quant']
        new_qty = vals['quantity']
        for res in self:
            if res.state != 'reserved':
                raise UserError(_('Quantity can only be edited on a Reserved line.'))
            if res.plan_id.state != 'confirmed':
                raise UserError(_('Quantity can only be edited while the Plan Order is Confirmed.'))
            rounding = uom_rounding(res.product_id.uom_id)
            if float_compare(new_qty, 0, precision_rounding=rounding) < 0:
                raise UserError(_('Quantity cannot be negative.'))
            delta = new_qty - res.quantity
            if float_is_zero(delta, precision_rounding=rounding):
                continue
            kw = dict(lot_id=res.lot_id, package_id=res.package_id, owner_id=res.owner_id, strict=True)
            if delta < 0:
                quants = Quant._gather(res.product_id, res.location_id, **kw)
                qty = min(-delta, sum(quants.mapped('reserved_quantity')))
                if float_compare(qty, 0, precision_rounding=rounding) > 0:
                    quant_unreserve(self.env, res.product_id, res.location_id, qty,
                                    lot=res.lot_id, package=res.package_id, owner=res.owner_id)
            else:
                available = Quant._get_available_quantity(res.product_id, res.location_id, **kw)
                if float_compare(delta, available, precision_rounding=rounding) > 0:
                    raise UserError(_(
                        '%s: only %s available at %s, cannot hold %s more.')
                        % (res.product_id.display_name, available, res.location_id.display_name, delta))
                Quant._update_reserved_quantity(res.product_id, res.location_id, delta, lot_id=res.lot_id,
                                                package_id=res.package_id, owner_id=res.owner_id, strict=True)
        res_vals = dict(vals)
        if float_is_zero(new_qty, precision_rounding=0.000001):
            res_vals['state'] = 'released'
            res_vals['quantity_released'] = max(self.mapped('quantity'))
        result = super(PlanReservation, self.with_context(plan_skip_qty_sync=True)).write(res_vals)
        self._cascade_lower_levels()
        return result

    def unlink(self):
        if any(r.state == 'reserved' for r in self):
            self.filtered(lambda r: r.state == 'reserved')._release('released')
        return super().unlink()
