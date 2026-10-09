# -*- coding: utf-8 -*-
# Odoo 14.0 — Wizard adding SO / Claim / Demand lines to a Plan Order
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..models.tools import sale_line_uom


class PlanAddLineWizard(models.TransientModel):
    _name = 'plan.add.line.wizard'
    _description = 'Add demand lines to Plan Order'

    plan_id = fields.Many2one('plan.order', required=True)
    company_id = fields.Many2one(related='plan_id.company_id')
    source_type = fields.Selection([
        ('sale', 'Sale Order (confirmed)'),
        ('claim', 'Claim (approved)'),
        ('forecast', 'Demand / Forecast (confirmed)'),
    ], required=True, default='sale')
    sale_line_ids = fields.Many2many(
        'sale.order.line', string='Sale Order Lines',
        domain="[('company_id', '=', company_id), ('state', 'in', ('sale', 'done')),"
               " ('plan_remaining_qty', '>', 0)]")
    claim_line_ids = fields.Many2many(
        'plan.claim.line', string='Claim Lines',
        domain="[('company_id', '=', company_id), ('state', '=', 'approved'),"
               " ('plan_remaining_qty', '>', 0)]")
    demand_line_ids = fields.Many2many(
        'plan.demand.line', string='Demand Lines',
        domain="[('company_id', '=', company_id), ('state', '=', 'confirmed'),"
               " ('plan_remaining_qty', '>', 0)]")

    def action_add(self):
        self.ensure_one()
        plan = self.plan_id
        if plan.state != 'draft':
            raise UserError(_('Lines can only be added while the Plan Order is in Draft.'))
        vals_list = []
        seq = (max(plan.line_ids.mapped('sequence')) if plan.line_ids else 0) + 10
        if self.source_type == 'sale':
            for sol in self.sale_line_ids:
                if sol.plan_remaining_qty <= 0:
                    continue
                vals_list.append({
                    'plan_id': plan.id, 'sequence': seq, 'source_type': 'sale',
                    'sale_line_id': sol.id, 'product_id': sol.product_id.id,
                    'product_uom': sale_line_uom(sol).id, 'product_qty': sol.plan_remaining_qty,
                    'date_planned': sol.order_id.commitment_date or sol.order_id.expected_date
                    or fields.Date.context_today(self),
                })
                seq += 10
        elif self.source_type == 'claim':
            for cl in self.claim_line_ids:
                if cl.plan_remaining_qty <= 0:
                    continue
                vals_list.append({
                    'plan_id': plan.id, 'sequence': seq, 'source_type': 'claim',
                    'claim_line_id': cl.id, 'product_id': cl.product_id.id,
                    'product_uom': cl.product_uom_id.id, 'product_qty': cl.plan_remaining_qty,
                    'date_planned': cl.date_expected or fields.Date.context_today(self),
                })
                seq += 10
        else:
            for dl in self.demand_line_ids:
                if dl.plan_remaining_qty <= 0:
                    continue
                vals_list.append({
                    'plan_id': plan.id, 'sequence': seq, 'source_type': 'forecast',
                    'demand_line_id': dl.id, 'product_id': dl.product_id.id,
                    'product_uom': dl.product_uom_id.id, 'product_qty': dl.plan_remaining_qty,
                    'date_planned': dl.date_demand,
                })
                seq += 10
        if vals_list:
            self.env['plan.order.line'].create(vals_list)
        return {'type': 'ir.actions.act_window_close'}
