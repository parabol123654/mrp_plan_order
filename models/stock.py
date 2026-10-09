# -*- coding: utf-8 -*-
# Stamp plan_order_id on moves / MO / PO lines for traceability and auto-assign.
# Hooks use *args/**kwargs because their signatures differ between Odoo versions.
from odoo import fields, models


def _plan_id_from(env, args, kwargs):
    """Find the procurement `values` dict among the hook arguments and read plan_order_id."""
    plan_id = env.context.get('plan_order_id')
    if plan_id:
        return plan_id
    values = kwargs.get('values')
    if values is None:
        for arg in args:
            if isinstance(arg, dict):
                values = arg
                break
    return (values or {}).get('plan_order_id')


class StockRule(models.Model):
    _inherit = 'stock.rule'

    def _get_stock_move_values(self, *args, **kwargs):
        res = super()._get_stock_move_values(*args, **kwargs)
        plan_id = _plan_id_from(self.env, args, kwargs)
        if plan_id:
            res['plan_order_id'] = plan_id
        return res

    def _prepare_mo_vals(self, *args, **kwargs):
        res = super()._prepare_mo_vals(*args, **kwargs)
        plan_id = _plan_id_from(self.env, args, kwargs)
        if plan_id:
            res['plan_order_id'] = plan_id
        return res

    def _update_purchase_order_line(self, *args, **kwargs):
        res = super()._update_purchase_order_line(*args, **kwargs)
        plan_id = _plan_id_from(self.env, args, kwargs)
        line = kwargs.get('line') or args[-1]
        if plan_id and not line.plan_order_id:
            res['plan_order_id'] = plan_id
        return res


class StockMove(models.Model):
    _inherit = 'stock.move'

    plan_order_id = fields.Many2one('plan.order', string='Plan Order', index=True, copy=False)

    def _action_done(self, cancel_backorder=False):
        moves = super()._action_done(cancel_backorder=cancel_backorder)
        done = moves.filtered(lambda m: m.state == 'done' and m.plan_order_id)
        if done:
            plans = done.mapped('plan_order_id')
            # Just produced / received -> reserve right away for waiting moves of the same plan
            plans._assign_plan_moves(products=done.mapped('product_id'))
            plans._check_done()
        return moves

    def _action_cancel(self):
        res = super()._action_cancel()
        self.mapped('plan_order_id')._check_done()
        return res


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    plan_order_id = fields.Many2one('plan.order', string='Plan Order', index=True, copy=False)

    def _get_move_raw_values(self, *args, **kwargs):
        res = super()._get_move_raw_values(*args, **kwargs)
        if self.plan_order_id:
            res['plan_order_id'] = self.plan_order_id.id
        return res

    def _get_move_finished_values(self, *args, **kwargs):
        res = super()._get_move_finished_values(*args, **kwargs)
        if self.plan_order_id:
            res['plan_order_id'] = self.plan_order_id.id
        return res


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    plan_order_id = fields.Many2one('plan.order', string='Plan Order', index=True, copy=False)

    def _prepare_purchase_order_line_from_procurement(self, *args, **kwargs):
        res = super()._prepare_purchase_order_line_from_procurement(*args, **kwargs)
        plan_id = _plan_id_from(self.env, args, kwargs)
        if plan_id:
            res['plan_order_id'] = plan_id
        return res

    def _prepare_stock_moves(self, picking):
        res = super()._prepare_stock_moves(picking)
        if self.plan_order_id:
            for vals in res:
                vals['plan_order_id'] = self.plan_order_id.id
        return res
