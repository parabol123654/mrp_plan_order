# -*- coding: utf-8 -*-
# Odoo 14.0
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.tools import float_compare

from .tools import (group_values, procurement_runner, sale_line_dest_location, sale_line_procurement_values,
                    sale_line_uom, uom_rounding)


class PlanOrderLine(models.Model):
    _name = 'plan.order.line'
    _description = 'Plan Order Line'
    _order = 'plan_id, sequence, id'

    plan_id = fields.Many2one('plan.order', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10, help='Upper lines take stock first when reserving.')
    company_id = fields.Many2one(related='plan_id.company_id', store=True)
    warehouse_id = fields.Many2one(related='plan_id.warehouse_id', store=True)
    state = fields.Selection(related='plan_id.state', store=True)

    source_type = fields.Selection([
        ('sale', 'Sale Order'),
        ('claim', 'Claim'),
        ('forecast', 'Demand / Forecast'),
    ], required=True, default='sale')
    sale_line_id = fields.Many2one('sale.order.line', ondelete='restrict', index=True)
    sale_order_id = fields.Many2one(related='sale_line_id.order_id', store=True)
    claim_line_id = fields.Many2one('plan.claim.line', ondelete='restrict', index=True)
    claim_id = fields.Many2one(related='claim_line_id.claim_id', store=True)
    demand_line_id = fields.Many2one('plan.demand.line', ondelete='restrict', index=True)
    demand_id = fields.Many2one(related='demand_line_id.demand_id', store=True)
    origin = fields.Char(compute='_compute_origin', store=True)
    partner_id = fields.Many2one('res.partner', compute='_compute_origin', store=True)

    product_id = fields.Many2one('product.product', required=True, check_company=True)
    product_uom = fields.Many2one('uom.uom', required=True)
    product_qty = fields.Float(string='Quantity', required=True, digits='Product Unit of Measure')
    date_planned = fields.Date(string='Date Needed', required=True, default=fields.Date.context_today)
    need_delivery = fields.Boolean(compute='_compute_need_delivery', store=True)

    reservation_ids = fields.One2many('plan.reservation', 'plan_line_id')
    qty_reserved = fields.Float(compute='_compute_qty_reserved', string='FG Held',
                                digits='Product Unit of Measure')
    qty_short = fields.Float(compute='_compute_qty_reserved', string='FG Short',
                             digits='Product Unit of Measure')

    _qty_positive = models.Constraint('CHECK(product_qty > 0)', 'Quantity must be positive.')

    @api.depends('source_type', 'sale_line_id', 'claim_line_id', 'demand_line_id')
    def _compute_origin(self):
        for line in self:
            if line.source_type == 'sale' and line.sale_line_id:
                line.origin = line.sale_line_id.order_id.name
                line.partner_id = line.sale_line_id.order_id.partner_shipping_id
            elif line.source_type == 'claim' and line.claim_line_id:
                line.origin = line.claim_line_id.claim_id.name
                line.partner_id = line.claim_line_id.claim_id.partner_id
            elif line.source_type == 'forecast' and line.demand_line_id:
                line.origin = line.demand_line_id.demand_id.name
                line.partner_id = False
            else:
                line.origin = False
                line.partner_id = False

    @api.depends('source_type', 'claim_line_id.need_delivery')
    def _compute_need_delivery(self):
        for line in self:
            if line.source_type == 'sale':
                line.need_delivery = True
            elif line.source_type == 'claim':
                line.need_delivery = line.claim_line_id.need_delivery
            else:
                line.need_delivery = False

    @api.depends('reservation_ids.quantity', 'reservation_ids.state', 'product_qty', 'product_uom')
    def _compute_qty_reserved(self):
        for line in self:
            held = sum(line.reservation_ids.filtered(lambda r: r.state in ('reserved', 'consumed')).mapped('quantity'))
            need = line.product_uom._compute_quantity(line.product_qty, line.product_id.uom_id) \
                if line.product_uom and line.product_id else line.product_qty
            line.qty_reserved = held
            line.qty_short = max(need - held, 0.0)

    @api.constrains('source_type', 'sale_line_id', 'claim_line_id', 'demand_line_id')
    def _check_source(self):
        for line in self:
            if line.source_type == 'sale' and not line.sale_line_id:
                raise ValidationError(_('A Sale Order line must be selected for source type Sale Order.'))
            if line.source_type == 'claim' and not line.claim_line_id:
                raise ValidationError(_('A Claim line must be selected for source type Claim.'))
            if line.source_type == 'forecast' and not line.demand_line_id:
                raise ValidationError(_('A Demand line must be selected for source type Forecast.'))

    @api.constrains('product_qty', 'sale_line_id', 'claim_line_id', 'demand_line_id')
    def _check_over_plan(self):
        for line in self:
            src = line.sale_line_id or line.claim_line_id or line.demand_line_id
            if not src:
                continue
            others = src.plan_line_ids.filtered(lambda p: p.state != 'cancel')
            planned = sum(others.mapped('product_qty'))
            src_qty = src.product_uom_qty if line.sale_line_id else src.product_qty
            rounding = uom_rounding(line.product_id.uom_id) or 0.01
            if float_compare(planned, src_qty, precision_rounding=rounding) > 0:
                raise ValidationError(_(
                    'Total planned quantity of %s (%s) exceeds the source quantity (%s).')
                    % (src.product_id.display_name, planned, src_qty))

    @api.onchange('sale_line_id')
    def _onchange_sale_line_id(self):
        if self.sale_line_id:
            self.product_id = self.sale_line_id.product_id
            self.product_uom = sale_line_uom(self.sale_line_id)
            self.product_qty = self.sale_line_id.plan_remaining_qty
            self.date_planned = self.sale_line_id.order_id.commitment_date or \
                self.sale_line_id.order_id.expected_date or fields.Date.context_today(self)

    @api.onchange('claim_line_id')
    def _onchange_claim_line_id(self):
        if self.claim_line_id:
            self.product_id = self.claim_line_id.product_id
            self.product_uom = self.claim_line_id.product_uom_id
            self.product_qty = self.claim_line_id.plan_remaining_qty
            self.date_planned = self.claim_line_id.date_expected or fields.Date.context_today(self)

    @api.onchange('demand_line_id')
    def _onchange_demand_line_id(self):
        if self.demand_line_id:
            self.product_id = self.demand_line_id.product_id
            self.product_uom = self.demand_line_id.product_uom_id
            self.product_qty = self.demand_line_id.plan_remaining_qty
            self.date_planned = self.demand_line_id.date_demand

    # ------------------------------------------------------------------
    # Procurement (Run Plan)
    # ------------------------------------------------------------------
    def _get_date_planned(self):
        self.ensure_one()
        return fields.Datetime.to_datetime(self.date_planned)

    def _prepare_procurements(self):
        """Output procurement of the line: OUT delivery to the customer (sale / claim with
        need_delivery). Lines destined to WH/Stock (forecast / claim without delivery) create
        nothing here: their shortfall is already part of the plan-level shortfall."""
        self.ensure_one()
        Procurement = procurement_runner(self.env).Procurement
        plan = self.plan_id
        wh = plan.warehouse_id
        company = plan.company_id

        if self.source_type == 'sale':
            sol = self.sale_line_id
            values = sale_line_procurement_values(sol)
            # Force the delivery route (MTS) so we never go through MTO: the module nets the shortfall itself
            values.update({
                'route_ids': wh.delivery_route_id,
                'warehouse_id': wh,
                'plan_order_id': plan.id,
            })
            location = sale_line_dest_location(sol)
            return [Procurement(self.product_id, self.product_qty, self.product_uom, location,
                                sol.name, sol.order_id.name, company, values)]

        if self.source_type == 'claim' and self.need_delivery:
            claim = self.claim_line_id.claim_id
            values = dict(group_values(claim._get_procurement_group()), **{
                'warehouse_id': wh,
                'company_id': company,
                'date_planned': self._get_date_planned(),
                'partner_id': claim.partner_id.id,
                'route_ids': wh.delivery_route_id,
                'plan_order_id': plan.id,
            })
            location = claim.partner_id.property_stock_customer
            return [Procurement(self.product_id, self.product_qty, self.product_uom, location,
                                self.product_id.display_name, claim.name, company, values)]

        # forecast / claim without delivery: destination is WH/Stock -> the missing part is already
        # in the shortfall computed by _walk_requirements (MO/PO into stock); the held part is in stock.
        return []
