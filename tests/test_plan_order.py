# -*- coding: utf-8 -*-
# Odoo 14.0 — Luồng SO -> Plan confirm (hold theo BOM level) -> Run plan
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

# Comments are Vietnamese on purpose: they describe the business scenarios discussed with the planning team.


@tagged('post_install', '-at_install', 'mrp_plan_order')
class TestPlanOrder(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.plan_gate_sale_procurement = True
        cls.wh = cls.env['stock.warehouse'].search([('company_id', '=', cls.env.company.id)], limit=1)
        cls.stock = cls.wh.lot_stock_id
        manufacture = cls.wh.manufacture_pull_id.route_id
        buy = cls.wh.buy_pull_id.route_id
        cls.vendor = cls.env['res.partner'].create({'name': 'Vendor'})
        cls.customer = cls.env['res.partner'].create({'name': 'Customer'})
        Product = cls.env['product.product']
        # RAW (buy) -> WHITE (manufacture, 3 RAW) -> FG (manufacture, 2 WHITE)
        cls.raw = Product.create({
            'name': 'RAW wood', 'type': 'consu', 'is_storable': True,
            'seller_ids': [(0, 0, {'partner_id': cls.vendor.id, 'price': 1.0, 'min_qty': 0})],
        })
        cls.white = Product.create({
            'name': 'WHITE chair frame', 'type': 'consu', 'is_storable': True,})
        cls.fg = Product.create({
            'name': 'FG chair painted', 'type': 'consu', 'is_storable': True,
            'sale_ok': True, 'list_price': 100})
        Bom = cls.env['mrp.bom']
        Bom.create({'product_tmpl_id': cls.white.product_tmpl_id.id, 'product_qty': 1, 'type': 'normal',
                    'bom_line_ids': [(0, 0, {'product_id': cls.raw.id, 'product_qty': 3})]})
        Bom.create({'product_tmpl_id': cls.fg.product_tmpl_id.id, 'product_qty': 1, 'type': 'normal',
                    'bom_line_ids': [(0, 0, {'product_id': cls.white.id, 'product_qty': 2})]})
        Quant = cls.env['stock.quant']
        Quant._update_available_quantity(cls.fg, cls.stock, 2)
        Quant._update_available_quantity(cls.white, cls.stock, 5)

    def _create_so(self, qty=10):
        so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'warehouse_id': self.wh.id,
            'order_line': [(0, 0, {'product_id': self.fg.id, 'product_uom_qty': qty})],
        })
        so.action_confirm()
        return so

    def _held(self, plan, product, level):
        return sum(plan.reservation_ids.filtered(
            lambda r: r.product_id == product and r.bom_level == level and r.state == 'reserved'
        ).mapped('quantity'))

    def test_01_so_confirm_is_gated(self):
        so = self._create_so()
        self.assertEqual(so.state, 'sale')
        self.assertFalse(so.picking_ids, 'SO confirm không được sinh OUT')
        self.assertFalse(self.env['mrp.production'].search([('origin', '=', so.name)]))
        self.assertEqual(so.order_line.plan_remaining_qty, 10)

    def test_02_confirm_holds_by_bom_level_and_run(self):
        so = self._create_so(10)
        plan = self.env['plan.order'].create({'warehouse_id': self.wh.id})
        wiz = self.env['plan.add.line.wizard'].create({
            'plan_id': plan.id, 'source_type': 'sale', 'sale_line_ids': [(6, 0, so.order_line.ids)]})
        wiz.action_add()
        self.assertEqual(len(plan.line_ids), 1)
        self.assertEqual(plan.line_ids.product_qty, 10)

        plan.action_confirm()
        self.assertEqual(plan.state, 'confirmed')
        # level 0: FG có 2 -> hold 2, thiếu 8 -> cần 16 WHITE, có 5 -> hold 5, thiếu 11 -> cần 33 RAW, có 0
        self.assertEqual(self._held(plan, self.fg, 0), 2)
        self.assertEqual(self._held(plan, self.white, 1), 5)
        self.assertEqual(self._held(plan, self.raw, 2), 0)
        self.assertEqual(plan.line_ids.qty_reserved, 2)
        self.assertEqual(plan.line_ids.qty_short, 8)
        # reserved thật trên quant
        Quant = self.env['stock.quant']
        self.assertEqual(Quant._get_available_quantity(self.fg, self.stock), 0)
        self.assertEqual(Quant._get_available_quantity(self.white, self.stock), 0)
        self.assertEqual(self.fg.with_context(location=self.stock.id).free_qty, 0)

        # nhả 1 dòng WHITE rồi hold lại
        white_res = plan.reservation_ids.filtered(lambda r: r.product_id == self.white)
        white_res.action_release()
        self.assertEqual(Quant._get_available_quantity(self.white, self.stock), 5)
        plan.reservation_ids.filtered(
            lambda r: r.product_id == self.white and r.state == 'released').action_hold()
        self.assertEqual(self._held(plan, self.white, 1), 5)

        plan.action_run_plan()
        self.assertEqual(plan.state, 'running')
        self.assertTrue(all(r.state == 'consumed' for r in plan.reservation_ids))

        # OUT giao khách 10 FG, gắn sale line, reserve được 2 từ hold cũ
        out_moves = plan.move_ids.filtered(lambda m: m.location_dest_id.usage == 'customer')
        self.assertEqual(len(out_moves), 1)
        self.assertEqual(out_moves.product_uom_qty, 10)
        self.assertEqual(out_moves.sale_line_id, so.order_line)
        self.assertEqual(out_moves.quantity, 2)
        self.assertEqual(so.picking_ids, out_moves.picking_id)

        # MO FG = 8 (chỉ phần thiếu), MO WHITE = 11
        mo_fg = plan.production_ids.filtered(lambda m: m.product_id == self.fg)
        mo_white = plan.production_ids.filtered(lambda m: m.product_id == self.white)
        self.assertEqual(mo_fg.product_qty, 8)
        self.assertEqual(mo_white.product_qty, 11)
        # raw move của MO FG (16 WHITE) reserve được 5 từ hold cũ
        self.assertEqual(mo_fg.move_raw_ids.product_uom_qty, 16)
        self.assertEqual(mo_fg.move_raw_ids.quantity, 5)
        # PO RAW = 33
        po_lines = self.env['purchase.order.line'].search([('plan_order_id', '=', plan.id)])
        self.assertEqual(po_lines.product_id, self.raw)
        self.assertEqual(po_lines.product_qty, 33)
        self.assertEqual(plan.purchase_count, 1)

    def test_05_edit_hold_quantity(self):
        """Available 5 WHITE, demand cần 5 nhưng planner chỉ muốn hold 3."""
        Quant = self.env['stock.quant']
        demand = self.env['plan.demand'].create({
            'warehouse_id': self.wh.id,
            'line_ids': [(0, 0, {'product_id': self.white.id, 'product_qty': 5})]})
        demand.action_confirm()
        plan = self.env['plan.order'].create({'warehouse_id': self.wh.id})
        wiz = self.env['plan.add.line.wizard'].create({
            'plan_id': plan.id, 'source_type': 'forecast', 'demand_line_ids': [(6, 0, demand.line_ids.ids)]})
        wiz.action_add()
        plan.action_confirm()
        hold = plan.reservation_ids.filtered(lambda r: r.product_id == self.white)
        self.assertEqual(hold.quantity, 5)
        self.assertEqual(Quant._get_available_quantity(self.white, self.stock), 0)

        hold.quantity = 3          # giảm -> nhả 2
        self.assertEqual(hold.state, 'reserved')
        self.assertEqual(Quant._get_available_quantity(self.white, self.stock), 2)
        self.assertEqual(self.white.with_context(location=self.stock.id).free_qty, 2)

        hold.quantity = 4          # tăng lại 1 -> còn 1 available
        self.assertEqual(Quant._get_available_quantity(self.white, self.stock), 1)
        with self.assertRaises(Exception):
            hold.quantity = 10     # vượt available

        hold.quantity = 0          # về 0 -> released
        self.assertEqual(hold.state, 'released')
        self.assertEqual(Quant._get_available_quantity(self.white, self.stock), 5)

        # Run với hold 0: thiếu 5 WHITE -> MO 5
        plan.action_run_plan()
        mo = plan.production_ids.filtered(lambda m: m.product_id == self.white)
        self.assertEqual(mo.product_qty, 5)

    def test_03_forecast_demand_no_delivery(self):
        demand = self.env['plan.demand'].create({
            'warehouse_id': self.wh.id,
            'line_ids': [(0, 0, {'product_id': self.fg.id, 'product_qty': 5})],
        })
        demand.action_confirm()
        plan = self.env['plan.order'].create({'warehouse_id': self.wh.id})
        wiz = self.env['plan.add.line.wizard'].create({
            'plan_id': plan.id, 'source_type': 'forecast',
            'demand_line_ids': [(6, 0, demand.line_ids.ids)]})
        wiz.action_add()
        plan.action_confirm()
        self.assertEqual(self._held(plan, self.fg, 0), 2)
        plan.action_run_plan()
        self.assertFalse(plan.move_ids.filtered(lambda m: m.location_dest_id.usage == 'customer'))
        mo_fg = plan.production_ids.filtered(lambda m: m.product_id == self.fg)
        self.assertEqual(mo_fg.product_qty, 3)   # 5 - 2 có sẵn
        self.assertEqual(demand.state, 'planned')

    def test_04_claim_replace_delivery(self):
        claim = self.env['plan.claim'].create({
            'partner_id': self.customer.id, 'warehouse_id': self.wh.id,
            'line_ids': [(0, 0, {'product_id': self.fg.id, 'product_qty': 1, 'need_delivery': True})],
        })
        claim.action_submit()
        claim.claim_type = 'replace'
        claim.action_approve()
        self.assertEqual(claim.state, 'approved')
        plan = self.env['plan.order'].create({'warehouse_id': self.wh.id})
        wiz = self.env['plan.add.line.wizard'].create({
            'plan_id': plan.id, 'source_type': 'claim', 'claim_line_ids': [(6, 0, claim.line_ids.ids)]})
        wiz.action_add()
        plan.action_confirm()
        self.assertEqual(self._held(plan, self.fg, 0), 1)
        plan.action_run_plan()
        out = plan.move_ids.filtered(lambda m: m.location_dest_id.usage == 'customer')
        self.assertEqual(out.product_uom_qty, 1)
        self.assertEqual(out.picking_id.partner_id, self.customer)
        self.assertFalse(plan.production_ids)   # đủ hàng, không cần MO
        self.assertEqual(claim.state, 'planned')

    def test_06_cascade_parent_child(self):
        """Giảm hold FG -> hold thêm WHITE; tăng lại -> nhả WHITE dư."""
        Quant = self.env['stock.quant']
        Quant._update_available_quantity(self.white, self.stock, 25)   # WHITE = 30
        so = self._create_so(10)
        plan = self.env['plan.order'].create({'warehouse_id': self.wh.id})
        wiz = self.env['plan.add.line.wizard'].create({
            'plan_id': plan.id, 'source_type': 'sale', 'sale_line_ids': [(6, 0, so.order_line.ids)]})
        wiz.action_add()
        plan.action_confirm()
        # FG 2 hold -> thiếu 8 -> 16 WHITE hold (kho 30)
        self.assertEqual(self._held(plan, self.fg, 0), 2)
        self.assertEqual(self._held(plan, self.white, 1), 16)
        self.assertEqual(Quant._get_available_quantity(self.white, self.stock), 14)

        fg_hold = plan.reservation_ids.filtered(lambda r: r.product_id == self.fg)
        fg_hold.quantity = 0                      # sản xuất mới hết 10
        self.assertEqual(self._held(plan, self.white, 1), 20)
        self.assertEqual(Quant._get_available_quantity(self.white, self.stock), 10)
        self.assertEqual(Quant._get_available_quantity(self.fg, self.stock), 2)

        fg_hold.action_hold()                     # hold lại 2 FG -> WHITE về 16
        self.assertEqual(self._held(plan, self.fg, 0), 2)
        self.assertEqual(self._held(plan, self.white, 1), 16)
        self.assertEqual(Quant._get_available_quantity(self.white, self.stock), 14)

        fg_hold = plan.reservation_ids.filtered(lambda r: r.product_id == self.fg and r.state == 'reserved')
        fg_hold.quantity = 1                      # -> thiếu 9 -> 18 WHITE
        self.assertEqual(self._held(plan, self.white, 1), 18)

        plan.action_run_plan()
        mo_fg = plan.production_ids.filtered(lambda m: m.product_id == self.fg)
        self.assertEqual(mo_fg.product_qty, 9)
        self.assertEqual(mo_fg.move_raw_ids.quantity, 18)
        self.assertFalse(plan.production_ids.filtered(lambda m: m.product_id == self.white))
