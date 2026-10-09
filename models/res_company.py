# -*- coding: utf-8 -*-
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    plan_gate_sale_procurement = fields.Boolean(
        string='Gate SO procurement (run through Plan Order)', default=True,
        help='When enabled, confirming a Sale Order does NOT launch stock rules. '
             'Procurements are created only when the planner runs the Plan Order.')


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    plan_gate_sale_procurement = fields.Boolean(
        related='company_id.plan_gate_sale_procurement', readonly=False)
