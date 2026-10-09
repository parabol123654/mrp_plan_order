# -*- coding: utf-8 -*-
# Odoo 14.0 — Gate SO confirmation: stock rules only run at Run Plan
from odoo import api, fields, models

from .tools import storable_domain


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    plan_line_ids = fields.One2many('plan.order.line', 'sale_line_id')
    plan_qty = fields.Float(compute='_compute_plan_qty', string='Planned Qty',
                            digits='Product Unit of Measure')
    plan_remaining_qty = fields.Float(compute='_compute_plan_qty', string='Unplanned Qty',
                                      digits='Product Unit of Measure',
                                      search='_search_plan_remaining_qty')

    @api.depends('plan_line_ids.product_qty', 'plan_line_ids.state', 'product_uom_qty')
    def _compute_plan_qty(self):
        for line in self:
            planned = sum(line.plan_line_ids.filtered(
                lambda p: p.state != 'cancel').mapped('product_qty'))
            line.plan_qty = planned
            line.plan_remaining_qty = line.product_uom_qty - planned

    def _search_plan_remaining_qty(self, operator, value):
        lines = self.search([('state', 'in', ('sale', 'done'))] + [
            ('product_id.' + k, op, v) for k, op, v in storable_domain()])
        ids = [l.id for l in lines if l.plan_remaining_qty > 0]
        if operator in ('>', '>='):
            return [('id', 'in', ids)]
        return [('id', 'not in', ids)]

    def _action_launch_stock_rule(self, *args, **kwargs):
        # *args/**kwargs: keyword-only signature since 19.0
        if self.env.context.get('plan_run'):
            return super()._action_launch_stock_rule(*args, **kwargs)
        # Skip lines of companies with the gate enabled: the planner will Run Plan later
        lines = self.filtered(lambda l: not l.company_id.plan_gate_sale_procurement)
        if not lines:
            return True
        return super(SaleOrderLine, lines)._action_launch_stock_rule(*args, **kwargs)
