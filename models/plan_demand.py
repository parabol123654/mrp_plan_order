# -*- coding: utf-8 -*-
# Odoo 14.0 — Demand Order (forecast / make-to-stock) created by Planning
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .tools import storable_domain


class PlanDemand(models.Model):
    _name = 'plan.demand'
    _description = 'Demand Order (Forecast)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(default='New', copy=False, readonly=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    warehouse_id = fields.Many2one(
        'stock.warehouse', required=True, check_company=True,
        default=lambda self: self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1))
    user_id = fields.Many2one('res.users', string='Planner', default=lambda self: self.env.user)
    date_demand = fields.Date(string='Date Needed', required=True, default=fields.Date.context_today)
    origin = fields.Char(string='Forecast Source')
    note = fields.Text()
    state = fields.Selection([
        ('draft', 'Draft'), ('confirmed', 'Confirmed'),
        ('planned', 'Planned'), ('done', 'Done'), ('cancel', 'Cancelled'),
    ], default='draft', tracking=True)
    line_ids = fields.One2many('plan.demand.line', 'demand_id', copy=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('plan.demand') or 'New'
        return super().create(vals_list)

    def action_confirm(self):
        for rec in self:
            if not rec.line_ids:
                raise UserError(_('Demand %s has no lines.') % rec.name)
        self.write({'state': 'confirmed'})

    def action_cancel(self):
        if any(l.plan_line_ids.filtered(lambda p: p.state not in ('cancel',)) for l in self.line_ids):
            raise UserError(_('This demand is already planned in a Plan Order and cannot be cancelled.'))
        self.write({'state': 'cancel'})

    def action_draft(self):
        self.write({'state': 'draft'})

    def _check_planned(self):
        for rec in self:
            if rec.state == 'confirmed' and rec.line_ids and all(
                    l.plan_remaining_qty <= 0 for l in rec.line_ids):
                rec.state = 'planned'


class PlanDemandLine(models.Model):
    _name = 'plan.demand.line'
    _description = 'Demand Order Line'

    demand_id = fields.Many2one('plan.demand', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='demand_id.company_id', store=True)
    warehouse_id = fields.Many2one(related='demand_id.warehouse_id', store=True)
    state = fields.Selection(related='demand_id.state', store=True)
    date_demand = fields.Date(related='demand_id.date_demand', store=True)
    product_id = fields.Many2one(
        'product.product', required=True, check_company=True,
        domain=lambda self: storable_domain())
    product_uom_id = fields.Many2one(
        'uom.uom', string='UoM', required=True,
        default=lambda self: self.env.ref('uom.product_uom_unit', raise_if_not_found=False))
    product_qty = fields.Float(string='Quantity', required=True, default=1.0, digits='Product Unit of Measure')
    plan_line_ids = fields.One2many('plan.order.line', 'demand_line_id')
    plan_qty = fields.Float(compute='_compute_plan_qty', string='Planned Qty', digits='Product Unit of Measure')
    plan_remaining_qty = fields.Float(
        compute='_compute_plan_qty', string='Remaining Qty', digits='Product Unit of Measure',
        search='_search_plan_remaining_qty')

    @api.onchange('product_id')
    def _onchange_product_id(self):
        if self.product_id:
            self.product_uom_id = self.product_id.uom_id

    @api.depends('plan_line_ids.product_qty', 'plan_line_ids.state', 'product_qty')
    def _compute_plan_qty(self):
        for line in self:
            planned = sum(line.plan_line_ids.filtered(
                lambda p: p.state != 'cancel').mapped('product_qty'))
            line.plan_qty = planned
            line.plan_remaining_qty = line.product_qty - planned

    def _search_plan_remaining_qty(self, operator, value):
        lines = self.search([('state', '=', 'confirmed')])
        ids = [l.id for l in lines if l.plan_remaining_qty > 0]
        if operator in ('>', '>=') :
            return [('id', 'in', ids)]
        return [('id', 'not in', ids)]
