# -*- coding: utf-8 -*-
# Odoo 14.0 — Plan Order: gather demand, hold stock per BOM level, run plan
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_round

from .tools import (PROC_GROUP_MODEL, bom_find, create_proc_group, group_values, is_storable, procurement_runner,
                    bom_uom, uom_rounding)

MAX_BOM_LEVEL = 10


class PlanOrder(models.Model):
    _name = 'plan.order'
    _description = 'Plan Order'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(default='New', copy=False, readonly=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    warehouse_id = fields.Many2one(
        'stock.warehouse', required=True, check_company=True,
        default=lambda self: self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1))
    user_id = fields.Many2one('res.users', string='Planner', default=lambda self: self.env.user, tracking=True)
    date_plan = fields.Date(default=fields.Date.context_today, required=True)
    date_run = fields.Datetime(readonly=True, copy=False)
    note = fields.Text()
    state = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed (stock held)'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('cancel', 'Cancelled'),
    ], default='draft', tracking=True, copy=False)

    line_ids = fields.One2many('plan.order.line', 'plan_id', copy=True)
    reservation_ids = fields.One2many('plan.reservation', 'plan_id', copy=False)
    reservation_count = fields.Integer(compute='_compute_counts')
    procurement_group_id = fields.Many2one(PROC_GROUP_MODEL, copy=False)

    move_ids = fields.One2many('stock.move', 'plan_order_id')
    production_ids = fields.One2many('mrp.production', 'plan_order_id')
    picking_ids = fields.Many2many('stock.picking', compute='_compute_counts')
    purchase_ids = fields.Many2many('purchase.order', compute='_compute_counts')
    production_count = fields.Integer(compute='_compute_counts')
    picking_count = fields.Integer(compute='_compute_counts')
    purchase_count = fields.Integer(compute='_compute_counts')
    line_count = fields.Integer(compute='_compute_counts')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('plan.order') or 'New'
        return super().create(vals_list)

    def unlink(self):
        if any(p.state not in ('draft', 'cancel') for p in self):
            raise UserError(_('Only Draft / Cancelled Plan Orders can be deleted.'))
        self.mapped('reservation_ids')._release('released')
        return super().unlink()

    @api.depends('reservation_ids.state', 'move_ids', 'production_ids', 'line_ids')
    def _compute_counts(self):
        PoLine = self.env['purchase.order.line']
        for plan in self:
            plan.reservation_count = len(plan.reservation_ids.filtered(lambda r: r.state == 'reserved'))
            plan.picking_ids = plan.move_ids.mapped('picking_id')
            plan.purchase_ids = PoLine.search([('plan_order_id', '=', plan.id)]).mapped('order_id')
            plan.production_count = len(plan.production_ids)
            plan.picking_count = len(plan.picking_ids)
            plan.purchase_count = len(plan.purchase_ids)
            plan.line_count = len(plan.line_ids)

    def _get_procurement_group(self):
        self.ensure_one()
        if not self.procurement_group_id:
            self.procurement_group_id = create_proc_group(self.env, self.name)
        return self.procurement_group_id

    # ==================================================================
    # BOM explode (netting theo level)
    # ==================================================================
    def _bom_cache(self):
        """Return a BOM lookup function cached per product."""
        cache = {}

        def find(product):
            if product.id not in cache:
                cache[product.id] = bom_find(self.env, product, self.company_id.id)
            return cache[product.id]
        return find

    def _explode_bom_lines(self, product, qty, bom):
        """Explode one level: returns [(component, qty_in_component_uom)]."""
        res = []
        bom_qty_prod_uom = bom_uom(bom)._compute_quantity(bom.product_qty, product.uom_id)
        if not bom_qty_prod_uom:
            return res
        factor = qty / bom_qty_prod_uom
        for bl in bom.bom_line_ids:
            if bl._skip_bom_line(product):
                continue
            comp = bl.product_id
            comp_qty = bom_uom(bl)._compute_quantity(bl.product_qty * factor, comp.uom_id)
            comp_qty = float_round(comp_qty, precision_rounding=uom_rounding(comp.uom_id),
                                   rounding_method='UP')
            res.append((comp, comp_qty))
        return res

    def _walk_requirements(self, allocate):
        """Walk the BOM levels. `allocate(product, qty, level, parent, plan_line)` -> qty covered.
        Returns the shortfall {product: qty} over all levels: what has to be produced / bought."""
        self.ensure_one()
        find_bom = self._bom_cache()
        shortfall_total = defaultdict(float)
        # level 0: each plan line in priority order
        current = defaultdict(float)   # product -> qty to explode to the next level
        parent_map = {}                # component_id -> parent product (traceability)
        for line in self.line_ids.sorted(lambda l: (l.sequence, l.id)):
            product = line.product_id
            need = line.product_uom._compute_quantity(line.product_qty, product.uom_id)
            bom = find_bom(product)
            if bom and bom.type == 'phantom':
                # kit: never stocked itself -> explode fully
                for comp, cq in self._explode_bom_lines(product, need, bom):
                    current[comp] += cq
                    parent_map[comp.id] = product
                continue
            held = allocate(product, need, 0, None, line) if is_storable(product) else 0.0
            short = need - held
            if float_compare(short, 0, precision_rounding=uom_rounding(product.uom_id)) > 0:
                shortfall_total[product] += short
                if bom:
                    for comp, cq in self._explode_bom_lines(product, short, bom):
                        current[comp] += cq
                        parent_map[comp.id] = product
        # level >= 1
        level = 0
        while current and level < MAX_BOM_LEVEL:
            level += 1
            next_level = defaultdict(float)
            for product, qty in current.items():
                bom = find_bom(product)
                if bom and bom.type == 'phantom':
                    for comp, cq in self._explode_bom_lines(product, qty, bom):
                        next_level[comp] += cq
                        parent_map[comp.id] = product
                    continue
                held = allocate(product, qty, level, parent_map.get(product.id), None) \
                    if is_storable(product) else 0.0
                short = qty - held
                if float_compare(short, 0, precision_rounding=uom_rounding(product.uom_id)) <= 0:
                    continue
                shortfall_total[product] += short
                if bom:
                    for comp, cq in self._explode_bom_lines(product, short, bom):
                        next_level[comp] += cq
                        parent_map[comp.id] = product
            current = next_level
        return shortfall_total

    # ==================================================================
    # Confirm: hold stock
    # ==================================================================
    def _reserve_stock(self):
        self.ensure_one()
        Reservation = self.env['plan.reservation']
        location = self.warehouse_id.lot_stock_id
        vals_list = []

        def allocate(product, qty, level, parent, plan_line):
            reserved = Reservation._reserve_on_quants(product, location, qty)
            if reserved:
                vals_list.extend(Reservation._prepare_vals(
                    self, product, reserved, level=level, parent=parent, plan_line=plan_line))
            return sum(q for _quant, q in reserved)

        self._walk_requirements(allocate)
        if vals_list:
            Reservation.create(vals_list)

    def _reserve_lower_levels(self, from_level=0):
        """Keep the holds of levels <= from_level (as adjusted by the planner), release and re-hold
        every level > from_level from the new requirement. Used when the planner edits a parent hold:
        less parent -> children needed -> hold more children; more parent -> release surplus children."""
        self.ensure_one()
        if self.state != 'confirmed':
            return
        Reservation = self.env['plan.reservation']
        lower = self.reservation_ids.filtered(lambda r: r.bom_level > from_level)
        lower._release('released')
        lower.unlink()

        line_pool = defaultdict(float)   # plan_line_id -> FG qty held (level 0)
        pool = defaultdict(float)        # (product_id, level) -> qty held (level 1..from_level)
        for r in self.reservation_ids.filtered(lambda r: r.state == 'reserved'):
            if r.bom_level == 0 and r.plan_line_id:
                line_pool[r.plan_line_id.id] += r.quantity
            else:
                pool[(r.product_id.id, r.bom_level)] += r.quantity

        location = self.warehouse_id.lot_stock_id
        vals_list = []

        def allocate(product, qty, level, parent, plan_line):
            if level <= from_level:
                if level == 0 and plan_line:
                    take = min(line_pool.get(plan_line.id, 0.0), qty)
                    line_pool[plan_line.id] -= take
                    return take
                key = (product.id, level)
                take = min(pool.get(key, 0.0), qty)
                pool[key] -= take
                return take
            reserved = Reservation._reserve_on_quants(product, location, qty)
            if reserved:
                vals_list.extend(Reservation._prepare_vals(
                    self, product, reserved, level=level, parent=parent, plan_line=plan_line))
            return sum(q for _quant, q in reserved)

        self._walk_requirements(allocate)
        if vals_list:
            Reservation.create(vals_list)

    def action_reserve_lower_levels(self):
        for plan in self:
            if plan.state != 'confirmed':
                raise UserError(_('Recompute is only possible while the Plan Order is Confirmed.'))
            plan._reserve_lower_levels(0)
        return True

    def action_confirm(self):
        for plan in self:
            if plan.state != 'draft':
                raise UserError(_('Only Draft Plan Orders can be confirmed.'))
            if not plan.line_ids:
                raise UserError(_('The Plan Order has no lines.'))
            plan._reserve_stock()
            plan.state = 'confirmed'
        return True

    def action_reserve_again(self):
        """Release everything and hold again (new stock arrived before running)."""
        for plan in self:
            if plan.state != 'confirmed':
                raise UserError(_('Re-reserve is only possible while the Plan Order is Confirmed.'))
            plan.reservation_ids._release('released')
            plan.reservation_ids.unlink()
            plan._reserve_stock()
        return True

    def action_release_all(self):
        self.filtered(lambda p: p.state == 'confirmed').mapped('reservation_ids')._release('released')
        return True

    def action_reset_draft(self):
        for plan in self:
            if plan.state not in ('confirmed', 'cancel'):
                raise UserError(_('Cannot reset to Draft from the current state.'))
            plan.reservation_ids._release('released')
            plan.reservation_ids.unlink()
            plan.state = 'draft'
        return True

    def action_cancel(self):
        for plan in self:
            if plan.state == 'running':
                raise UserError(_('The plan is running: cancel the related MO / PO / transfers first, then cancel the plan.'))
            plan.reservation_ids._release('released')
            plan.state = 'cancel'
        return True

    # ==================================================================
    # Run plan
    # ==================================================================
    def action_run_plan(self):
        for plan in self:
            if plan.state != 'confirmed':
                raise UserError(_('Run Plan is only possible while the Plan Order is Confirmed.'))
            plan._run_plan()
        return True

    def _run_plan(self):
        self.ensure_one()
        Procurement = procurement_runner(self.env).Procurement
        wh = self.warehouse_id
        company = self.company_id

        # 1. Shortfall computed from the current holds (whatever the planner released is missing)
        pool = defaultdict(float)     # (product_id, level) -> qty held
        for r in self.reservation_ids.filtered(lambda r: r.state == 'reserved'):
            pool[(r.product_id.id, r.bom_level)] += r.quantity

        def allocate(product, qty, level, parent, plan_line):
            key = (product.id, level)
            take = min(pool.get(key, 0.0), qty)
            pool[key] = pool.get(key, 0.0) - take
            return take

        shortfall = self._walk_requirements(allocate)

        # 2. Release all holds: same transaction as step 4, nobody can grab the stock in between
        self.reservation_ids.filtered(lambda r: r.state == 'reserved')._release('consumed')

        # 3. Procurements
        procurements = []
        for line in self.line_ids:
            procurements += line._prepare_procurements()
        group = self._get_procurement_group()
        for product, qty in shortfall.items():
            qty = float_round(qty, precision_rounding=uom_rounding(product.uom_id), rounding_method='UP')
            if float_compare(qty, 0, precision_rounding=uom_rounding(product.uom_id)) <= 0:
                continue
            values = dict(group_values(group), **{
                'warehouse_id': wh,
                'company_id': company,
                'date_planned': fields.Datetime.now(),
                'plan_order_id': self.id,
            })
            # To WH/Stock -> Odoo picks Manufacture / Buy from the product routes
            procurements.append(Procurement(product, qty, product.uom_id, wh.lot_stock_id,
                                            product.display_name, self.name, company, values))
        if procurements:
            procurement_runner(self.env).with_context(
                plan_order_id=self.id, plan_run=True).run(procurements)

        # 4. Reserve the plan moves right away (OUT delivery, MO raw) from the released stock
        self._assign_plan_moves()

        self.write({'state': 'running', 'date_run': fields.Datetime.now()})
        self.line_ids.mapped('demand_id')._check_planned()
        self.line_ids.mapped('claim_id')._check_planned()
        if not self.move_ids:
            # forecast / claim into stock fully covered by stock: nothing to run
            self._check_done(force=True)
        return True

    def _assign_plan_moves(self, products=None):
        domain = [('plan_order_id', 'in', self.ids),
                  ('state', 'in', ('confirmed', 'partially_available', 'waiting'))]
        if products:
            domain.append(('product_id', 'in', products.ids))
        moves = self.env['stock.move'].search(domain)
        if moves:
            moves._action_assign()
        return moves

    def _check_done(self, force=False):
        for plan in self.filtered(lambda p: p.state == 'running'):
            moves = plan.move_ids
            if (moves or force) and all(m.state in ('done', 'cancel') for m in moves):
                plan.state = 'done'
                plan.line_ids.mapped('demand_id').filtered(lambda d: d.state == 'planned').write({'state': 'done'})
                plan.line_ids.mapped('claim_id').filtered(lambda c: c.state == 'planned').write({'state': 'done'})

    def action_check_done(self):
        self._check_done()
        return True

    # ==================================================================
    # Smart buttons
    # ==================================================================
    def action_open_add_line_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'plan.add.line.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_plan_id': self.id},
        }

    def _action_view(self, model, records, name):
        action = {'type': 'ir.actions.act_window', 'res_model': model, 'name': name,
                  'domain': [('id', 'in', records.ids)], 'view_mode': 'list,form'}
        if len(records) == 1:
            action.update({'view_mode': 'form', 'res_id': records.id})
        return action

    def action_view_reservations(self):
        return self._action_view('plan.reservation', self.reservation_ids, _('Stock holds'))

    def action_view_productions(self):
        return self._action_view('mrp.production', self.production_ids, _('Manufacturing Orders'))

    def action_view_pickings(self):
        return self._action_view('stock.picking', self.picking_ids, _('Transfers'))

    def action_view_purchases(self):
        return self._action_view('purchase.order', self.purchase_ids, _('Purchase Orders'))
