# Production Planning – Plan Order (BOM-level stock hold)

**Odoo 14.0 → 20.0 Community · LGPL-3 · English / Tiếng Việt**

[English](#english) · [Tiếng Việt](#tiếng-việt)

---

## English

### What it does

A planning layer between Sales and Manufacturing, built for made-to-order furniture where finished
goods are painted / polished / upholstered versions of **white (unfinished) components** kept in stock.

| Step | What happens |
|---|---|
| Sale Order confirmed | **Nothing** is launched (no OUT / MO / PO). The SO line shows *Planned Qty* / *Unplanned Qty*. |
| Plan Order – *Add SO / Claim / Demand* | Planner gathers confirmed SO lines, QA-approved Claim lines and confirmed Demand (forecast) lines. |
| **Confirm & Hold stock** | Stock is held **per BOM level**: finished goods first, the shortfall explodes to level-1 components, their shortfall to level 2… Holds are real `reserved_quantity` on quants – Inventory shows *Reserved*, other plans see less free stock. |
| Adjust | Edit *Quantity* of any hold line, *Release* / *Hold* a line. Changing a parent hold **cascades**: lower levels are re-held from the new requirement. |
| **Run Plan** | Holds are released and, in the same transaction, the plan's own moves reserve them. Sale Orders / Claims get an **OUT delivery**; forecast only produces into stock. One procurement per missing product goes to WH/Stock → Odoo picks **Manufacture or Buy** from the product routes → MO / PO **for the net shortfall only**. |
| Follow-up | When an MO or receipt is done, waiting moves of the same plan (OUT, parent MO raw moves) are reserved automatically. Smart buttons: Holds · MO · PO · Transfers. |

### Real-life example: the planner overrides the hold

> 5 finished chairs are in stock, a Sale Order needs 5. The planner knows 2 of them are showroom
> pieces / promised to another customer / in a colour about to be discontinued.

She opens *Stock holds (BOM level)* and changes the finished-good hold from **5 → 3**.
The module immediately holds the extra **white frames and timber** needed to **produce the 2 missing
chairs**; the 2 chairs she excluded become available again for whoever needs them.

> Later, 10 more chairs are received from production.

She raises the finished-good hold back to **5**. Fewer components are now needed, so the surplus
component holds are **released for other plans**. Nothing is produced twice.

Rules of the cascade:

- Editing a hold on level *n* keeps every level ≤ *n* as the planner set it and recomputes every level > *n*.
- **Recompute lower levels** does the same from level 0 on demand.
- **Re-reserve** forgets manual adjustments and holds again from current stock.
- The cascade goes **down only**: demand for finished goods comes from orders, it is never inferred from component edits.

### Why holds on quants instead of OCA `stock_reserve`

| | OCA `stock_reserve` | This module |
|---|---|---|
| Mechanism | One `stock.move` + picking per line, `action_assign` each | `stock.quant._update_reserved_quantity()` – the same core method `_action_assign` uses |
| Cost | N lines × M levels moves | 1 call per distinct product per level |
| BOM level netting | – | Yes, only the shortfall explodes down |
| Inventory *Reserved* | Correct | Correct |
| Release | Cancel the move | Per line, per quantity, cascade |

Releases are looked up strictly by (product, location, lot, package, owner) and capped at the
current `reserved_quantity`, so merged quants or moves done in the meantime never raise.

### Demand sources

- **Sale Order** – confirmed SO lines with storable products. Moves created at Run Plan are linked to the sale line (delivered qty, invoicing unchanged).
- **Demand Order** (Planning ▸ Demand Orders) – forecast / make-to-stock. Only produces into stock, never delivers.
- **Claim** (Planning ▸ Claims) – created by Customer Service, approved by QA/QC with a *Claim type* (credit note, replace, repair…). Each line has *Deliver to Customer*: on → OUT to the customer, off → produce / repair into stock. Credit-note-only claims need no plan.

### Setup

1. Planning ▸ Configuration ▸ Settings – keep **Gate SO procurement (run through Plan Order)** enabled (default).
2. Products – route **Manufacture** or **Buy**. Do **not** enable *Replenish on Order (MTO)*: the module nets the shortfall itself; MTO would create MOs for the full quantity (v14 `mts_else_mto` does not split).
3. Groups – *Planner*, *Planning Manager*, *Customer Service (Claim)*, *QA/QC (Approve claim)*.

### Version notes

The same source is built for every version by `build.py` (one branch per Odoo series on Odoo Apps). Python code is
version-agnostic through `models/tools.py`; only view syntax and a few renamed core objects differ.

| Odoo | What changes for this module |
|---|---|
| 14.0 | Reference implementation. `stock.quant._update_reserved_quantity()` returns the reserved quants. |
| 15.0 | `mrp.bom._bom_find()` batch API. Tests on `TransactionCase`. |
| 16.0 | `stock.lot` replaces `stock.production.lot`; `product.supplierinfo.partner_id`. |
| 17.0 | No more `attrs` / `states` (python expressions); settings as `<app>/<block>/<setting>`; reservation split into `_get_reserve_quantity()` + `_update_reserved_quantity()`; `stock.move.quantity`. |
| 18.0 | `<list>` views, `<chatter/>`; storable = `is_storable` (no more `type='product'`). |
| 19.0 | `stock.reference` + `stock.rule.run()` replace `procurement.group`; `sale.order.line.product_uom_id`; `res.groups.privilege`; `models.Constraint`; `stock.package`. |
| 20.0 | `ir.access.csv` replaces `ir.model.access.csv` + `ir.rule`; routes are no longer set on products — Odoo picks **Manufacture** when the product has a BoM and **Buy** when it has a vendor; `uom.uom.rounding` removed; BoM `uom_id`. |

Setup step 2 (routes) therefore only applies up to 19.0. On 20.0 just give the product a BoM or a vendor.

### Technical notes

- Depends on `sale_stock`, `mrp`, `purchase_stock`.
- `sale.order.line._action_launch_stock_rule()` is gated by `res.company.plan_gate_sale_procurement`.
- Moves, MOs and PO lines created by a plan carry `plan_order_id` (stamped in `stock.rule` hooks).
- Unit tests: `tests/test_plan_order.py` (SO gate, BOM-level hold, quantity edit, cascade, forecast, claim) — run green on 14.0, 15.0, 16.0, 17.0, 18.0, 19.0 and 20.0.
- Design notes (Vietnamese): `doc/DESIGN.vi.md`.

```bash
odoo -d <db> -u mrp_plan_order --test-enable --test-tags /mrp_plan_order --stop-after-init
```

---

## Tiếng Việt

### Module làm gì

Lớp lập kế hoạch nằm giữa Sales và Sản xuất, thiết kế cho nội thất làm theo đơn: thành phẩm là
**component trắng (chưa hoàn thiện)** trong kho được sơn / đánh bóng / bọc nệm.

| Bước | Điều gì xảy ra |
|---|---|
| Xác nhận Sale Order | **Không** sinh gì cả (không OUT / MO / PO). Dòng SO hiện *Đã plan* / *Chưa plan*. |
| Plan Order – *Thêm SO / Claim / Demand* | Planner gom dòng SO đã xác nhận, dòng Claim QA đã duyệt, dòng Demand (dự báo) đã xác nhận. |
| **Xác nhận & Giữ hàng** | Giữ hàng **theo từng BOM level**: thành phẩm trước, phần thiếu nổ xuống component level 1, phần thiếu của component nổ xuống level 2… Giữ ghi thật lên `reserved_quantity` của quant – Inventory hiện *Reserved*, plan khác thấy ít hàng hơn. |
| Chỉnh | Sửa *Số lượng* bất kỳ dòng giữ, *Nhả* / *Giữ* cả dòng. Sửa ở cha **cascade** xuống: level dưới được giữ lại theo nhu cầu mới. |
| **Chạy kế hoạch** | Nhả giữ và ngay trong cùng transaction, move của plan reserve lại. SO / Claim có **phiếu OUT giao khách**; dự báo chỉ sản xuất nhập kho. Mỗi sản phẩm thiếu một procurement tới WH/Stock → Odoo chọn **Manufacture hay Buy** theo route sản phẩm → MO / PO **chỉ cho phần thiếu**. |
| Theo dõi | MO / phiếu nhận xong → move đang chờ của cùng plan (OUT, MO cha) được reserve tự động. Smart button: Giữ · MO · PO · Phiếu kho. |

### Ví dụ thực tế: planner chỉnh tay số giữ

> Kho có 5 ghế thành phẩm, đơn hàng cần 5. Planner biết 2 cái là hàng showroom / đã hứa cho khách
> khác / màu cũ sắp đổi.

Planner mở *Hàng đang giữ (theo BOM level)*, sửa giữ thành phẩm **5 → 3**. Module lập tức giữ thêm
**khung trắng và gỗ** để **sản xuất mới 2 cái**; 2 ghế planner loại ra trở lại available cho ai cần.

> Sau đó sản xuất nhập kho thêm 10 ghế.

Planner tăng giữ thành phẩm lên lại **5**. Nhu cầu component giảm → module **nhả component dư** ra cho
plan khác. Không có gì bị sản xuất hai lần.

Quy tắc cascade:

- Sửa giữ ở level *n*: giữ nguyên mọi level ≤ *n* như planner đặt, tính lại mọi level > *n*.
- **Tính lại level dưới** làm việc đó từ level 0 khi bấm.
- **Giữ lại từ đầu** bỏ hết chỉnh tay, giữ lại theo tồn kho hiện tại.
- Cascade **chỉ đi xuống**: nhu cầu thành phẩm đến từ đơn hàng, không suy ngược từ component.

### Vì sao giữ trên quant thay cho OCA `stock_reserve`

| | OCA `stock_reserve` | Module này |
|---|---|---|
| Cơ chế | 1 `stock.move` + phiếu cho mỗi dòng, `action_assign` từng cái | `stock.quant._update_reserved_quantity()` – đúng hàm core `_action_assign` dùng |
| Chi phí | N dòng × M level move | 1 call cho mỗi sản phẩm mỗi level |
| Netting theo BOM level | – | Có, chỉ phần thiếu nổ xuống |
| Inventory *Reserved* | Đúng | Đúng |
| Nhả | Huỷ move | Theo dòng, theo số lượng, cascade |

Nhả tra theo (sản phẩm, vị trí, lô, kiện, chủ sở hữu) strict và cap ở `reserved_quantity` hiện có,
nên quant bị cron merge hay move khác đã done cũng không gây lỗi.

### Nguồn nhu cầu

- **Sale Order** – dòng SO đã xác nhận, sản phẩm lưu kho. Move sinh khi chạy plan gắn `sale_line_id` (giao hàng, hoá đơn như thường).
- **Demand Order** (Planning ▸ Demand Orders) – dự báo / sản xuất nhập kho, không giao.
- **Claim** (Planning ▸ Claims) – CS tạo, QA/QC duyệt với *Loại claim* (credit note, thay, sửa…). Mỗi dòng có *Cần giao*: bật → OUT giao khách, tắt → sản xuất / sửa nhập kho. Claim chỉ credit note không cần plan.

### Cài đặt

1. Planning ▸ Cấu hình ▸ Thiết lập – giữ bật **Chặn SO tự sinh MO/PO (chạy qua Plan Order)** (mặc định).
2. Sản phẩm – route **Manufacture** hoặc **Buy**. **Không** bật *Replenish on Order (MTO)*: module tự netting; MTO sẽ tạo MO cho full số lượng (v14 `mts_else_mto` không tách).
3. Nhóm quyền – *Planner*, *Quản lý kế hoạch*, *Chăm sóc khách hàng (Claim)*, *QA/QC (duyệt claim)*.

### Ghi chú theo version

Một nguồn duy nhất, `build.py` sinh ra bản cho từng version (mỗi series Odoo một branch trên Odoo Apps). Code Python
tự nhận version qua `models/tools.py`; chỉ cú pháp view và vài object core đổi tên là khác nhau — xem bảng ở phần
tiếng Anh. Lưu ý riêng **20.0**: không còn gán route lên sản phẩm; Odoo tự chọn **Manufacture** nếu sản phẩm có BoM,
**Buy** nếu có nhà cung cấp, nên bước cài đặt số 2 chỉ áp dụng tới 19.0.

### Ghi chú kỹ thuật

- Phụ thuộc `sale_stock`, `mrp`, `purchase_stock`.
- `sale.order.line._action_launch_stock_rule()` bị chặn bởi `res.company.plan_gate_sale_procurement`.
- Move, MO, dòng PO sinh từ plan mang `plan_order_id` (đóng dấu trong hook `stock.rule`).
- Unit test: `tests/test_plan_order.py`. Thiết kế chi tiết: `doc/DESIGN.vi.md`.

---

Maintainer: District Eight · manh.nv@districteight.com
