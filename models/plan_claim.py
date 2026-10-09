# -*- coding: utf-8 -*-
# Odoo 14.0 — Claim: created by CS, approved by QA/QC, planned by the planner
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .tools import PROC_GROUP_MODEL, create_proc_group, storable_domain


class PlanClaim(models.Model):
    _name = 'plan.claim'
    _description = 'Customer Claim'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(default='New', copy=False, readonly=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    partner_id = fields.Many2one('res.partner', string='Customer', required=True)
    sale_order_id = fields.Many2one('sale.order', string='Original Sale Order',
                                    domain="[('partner_id', 'child_of', partner_id)]")
    warehouse_id = fields.Many2one(
        'stock.warehouse', required=True, check_company=True,
        default=lambda self: self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1))
    date_claim = fields.Date(default=fields.Date.context_today, required=True)
    date_expected = fields.Date(string='Expected Delivery Date')
    user_id = fields.Many2one('res.users', string='CS', default=lambda self: self.env.user)
    qa_user_id = fields.Many2one('res.users', string='QA/QC', readonly=True, copy=False)
    qa_date = fields.Datetime(readonly=True, copy=False)
    claim_type = fields.Selection([
        ('credit', 'Credit note'),
        ('replace', 'Replace component / product'),
        ('repair', 'Repair'),
        ('replace_repair', 'Replace + Repair'),
        ('credit_replace', 'Credit note + Replace/Repair'),
    ], string='Claim type', tracking=True)
    description = fields.Text(string='Defect Description')
    qa_note = fields.Text(string='QA/QC Conclusion')
    state = fields.Selection([
        ('draft', 'Draft'), ('submitted', 'Submitted'),
        ('approved', 'Approved'), ('rejected', 'Rejected'),
        ('planned', 'Planned'), ('done', 'Done'),
    ], default='draft', tracking=True)
    line_ids = fields.One2many('plan.claim.line', 'claim_id', copy=True)
    procurement_group_id = fields.Many2one(PROC_GROUP_MODEL, copy=False)
    need_plan = fields.Boolean(compute='_compute_need_plan')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('plan.claim') or 'New'
        return super().create(vals_list)

    @api.depends('claim_type')
    def _compute_need_plan(self):
        for rec in self:
            rec.need_plan = rec.claim_type not in (False, 'credit')

    def action_submit(self):
        self.write({'state': 'submitted'})

    def action_approve(self):
        for rec in self:
            if not rec.claim_type:
                raise UserError(_('QA/QC must set the Claim type before approving.'))
            if rec.need_plan and not rec.line_ids:
                raise UserError(_('This claim requires production / repair but has no product lines.'))
        self.write({'state': 'approved', 'qa_user_id': self.env.uid, 'qa_date': fields.Datetime.now()})
        # credit note only -> nothing to plan, planning side is done
        self.filtered(lambda r: not r.need_plan).write({'state': 'done'})

    def action_reject(self):
        self.write({'state': 'rejected', 'qa_user_id': self.env.uid, 'qa_date': fields.Datetime.now()})

    def action_draft(self):
        self.write({'state': 'draft'})

    def _get_procurement_group(self):
        self.ensure_one()
        if not self.procurement_group_id:
            self.procurement_group_id = create_proc_group(self.env, self.name, self.partner_id)
        return self.procurement_group_id

    def _check_planned(self):
        for rec in self:
            if rec.state == 'approved' and rec.line_ids and all(
                    l.plan_remaining_qty <= 0 for l in rec.line_ids):
                rec.state = 'planned'


class PlanClaimLine(models.Model):
    _name = 'plan.claim.line'
    _description = 'Claim Line'

    claim_id = fields.Many2one('plan.claim', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='claim_id.company_id', store=True)
    partner_id = fields.Many2one(related='claim_id.partner_id', store=True)
    warehouse_id = fields.Many2one(related='claim_id.warehouse_id', store=True)
    state = fields.Selection(related='claim_id.state', store=True)
    date_expected = fields.Date(related='claim_id.date_expected', store=True)
    product_id = fields.Many2one('product.product', required=True, check_company=True,
                                 domain=lambda self: storable_domain())
    product_uom_id = fields.Many2one(
        'uom.uom', string='UoM', required=True,
        default=lambda self: self.env.ref('uom.product_uom_unit', raise_if_not_found=False))
    product_qty = fields.Float(string='Quantity', required=True, default=1.0, digits='Product Unit of Measure')
    action_type = fields.Selection([('replace', 'Replace'), ('repair', 'Repair')],
                                   default='replace', required=True)
    need_delivery = fields.Boolean(
        string='Deliver to Customer', default=True,
        help='On: Run Plan creates an OUT delivery to the customer. Off: only produce / repair into stock.')
    description = fields.Char()
    plan_line_ids = fields.One2many('plan.order.line', 'claim_line_id')
    plan_qty = fields.Float(compute='_compute_plan_qty', string='Planned Qty', digits='Product Unit of Measure')
    plan_remaining_qty = fields.Float(compute='_compute_plan_qty', string='Remaining Qty',
                                      digits='Product Unit of Measure',
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
        lines = self.search([('state', '=', 'approved')])
        ids = [l.id for l in lines if l.plan_remaining_qty > 0]
        if operator in ('>', '>='):
            return [('id', 'in', ids)]
        return [('id', 'not in', ids)]
