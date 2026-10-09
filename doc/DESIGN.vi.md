# Thiết kế kỹ thuật — Module `mrp_plan_order` (Odoo 14.0)

Furniture manufacturing · gom Sale Order / Demand Order / Claim → reserve theo BOM level → Run Plan.

---

## 1. Ý tưởng cốt lõi (khác OCA `stock_reserve`)

| | OCA `stock_reserve` | Module này |
|---|---|---|
| Cách reserve | Tạo 1 `stock.move` + 1 picking cho **mỗi** line, rồi `action_assign` từng cái | Gọi thẳng `stock.quant._update_reserved_quantity()` (core, cùng hàm mà `stock.move._action_assign` dùng) — **1 call / product**, không tạo move/picking |
| Số quant ghi | N line × M level move | Gom demand theo (product, level) trước, rồi mới đụng quant |
| BOM level | Không có | Explode BOM netting từng level: chỉ phần **thiếu** ở level trên mới explode xuống level dưới |
| Hiển thị ở Inventory | Reserved qty đúng | Reserved qty đúng (vì ghi thẳng `stock_quant.reserved_quantity`) |
| Nhả / giữ | Cancel move | Bấm Release / Hold trên từng dòng `plan.reservation` |
| Khi run | Move reserve giữ luôn | Hold được nhả trong **cùng transaction** rồi move thật của plan `_action_assign()` ngay → không ai chen được |

Reserved qty của **plan khác** vẫn nằm trên quant → khi plan này chạy, stock available đã bị trừ → số lượng sản xuất / mua tính đúng.

## 2. Data model

```
plan.order                 PLAN/00001 — header, warehouse, state
 ├─ plan.order.line        1 dòng = 1 demand FG: source_type = sale | claim | forecast
 │     sale_line_id / claim_line_id / demand_line_id, product, qty, date
 └─ plan.reservation       1 dòng = 1 hold trên 1 quant
       product, parent_product, bom_level (0 = FG), location/lot/package/owner, quantity,
       state = reserved | released | consumed

plan.demand / plan.demand.line       Demand Order (Planning tạo, forecast → make-to-stock)
plan.claim  / plan.claim.line        Claim (CS tạo → QA/QC approve → claim_type + từng line need_delivery)

Stamp để trace:  stock.move.plan_order_id, mrp.production.plan_order_id, purchase.order.line.plan_order_id
Sale:            sale.order.line.plan_line_ids, plan_qty, plan_remaining_qty
Company:         plan_gate_sale_procurement (chặn SO confirm tự sinh MO/PO)
```

### State flow

```
plan.order:   draft ──confirm──▶ confirmed ──run_plan──▶ running ──(mọi move done)──▶ done
                 ▲                  │ reset_draft (nhả hết)                 cancel
                 └──────────────────┘
plan.demand:  draft → confirmed → planned → done / cancel
plan.claim:   draft → submitted (CS) → approved (QA/QC) → planned → done | rejected
```

## 3. Chặn luồng SO confirm

Override `sale.order.line._action_launch_stock_rule()`: nếu `company.plan_gate_sale_procurement` bật và không có context `plan_run` → **return** (không chạy procurement). SO confirm xong chỉ ở trạng thái Sales Order, chưa có OUT/MO. Planner add SO line vào plan (wizard chỉ liệt kê line còn `plan_remaining_qty > 0`).

## 4. Thuật toán Confirm plan = reserve theo BOM level

```
level 0:  với từng plan line (theo thứ tự ưu tiên):
            need = qty FG
            held = min(need, quant available tại WH/Stock)  → _update_reserved_quantity()
            short[FG] += need - held
level n+1: với mỗi (product, short) ở level n:
            bom = mrp.bom._bom_find(product)      # không có BOM (mua ngoài) → dừng, để procurement lo
            phantom → không reserve chính nó, explode full
            với mỗi bom_line (bỏ _skip_bom_line):  comp_need = short × qty_bom_line / bom.qty
                held = reserve comp   ;  short_next[comp] += comp_need - held
lặp tới khi không còn short hoặc level > 10
```

- Gom `short` theo product trước khi qua level kế → số call quant = số product distinct, không phải số line.
- Tạo `plan.reservation` bằng **một** `create([...])`.
- Chỉ reserve `type='product'`; consumable/service bỏ qua nhưng vẫn explode nếu có BOM.
- Nút **Re-reserve**: nhả hết + chạy lại (dùng khi kho có hàng mới về trước lúc run).
- Release/Hold từng dòng: nhả → hàng đó thành available cho plan khác; hold lại → reserve lại nếu còn.
- **Sửa Quantity trực tiếp** trên dòng hold (plan Confirmed): giảm → nhả đúng phần chênh trên quant (vd available 5, demand 5, sửa còn 3 → 2 available lại cho plan khác); tăng → hold thêm nếu còn available, không thì báo lỗi; về 0 → dòng Released. **Cascade cha → con**: mỗi khi sửa / nhả / hold một dòng ở level *n*, module giữ nguyên level ≤ *n* (số planner đã chỉnh) và tính lại + hold lại toàn bộ level > *n*: giảm hold FG → hold thêm component cho phần phải sản xuất mới; tăng hold FG → nhả component dư. Nút *Recompute lower levels* chạy tay từ level 0; *Re-reserve* thì bỏ hết chỉnh tay, hold lại theo tồn kho.

## 5. Run Plan

1. **Tính shortfall từ holds** (không đọc lại kho): chạy lại explode nhưng "available" = pool hold còn `reserved` của plan theo (product, level). Planner đã nhả gì thì phần đó thành thiếu → sẽ sản xuất / mua.
2. **Nhả toàn bộ hold** của plan (state → `consumed`).
3. **Procurement** (một `procurement.group.run()` cho cả plan, context `plan_order_id`):
   - `sale`: `sale_line._prepare_procurement_values()` chuẩn core + `route_ids = warehouse.delivery_route_id` → **OUT-** giao khách, move gắn `sale_line_id` (delivered qty, invoice chạy bình thường).
   - `claim` line `need_delivery`: procurement tới `partner.property_stock_customer`, group riêng của claim → **OUT-**. Không need_delivery → tới WH/Stock.
   - `forecast` / claim không giao: **không** tạo procurement riêng — đích là WH/Stock nên phần thiếu đã nằm trong shortfall bên dưới (tránh double MO), phần đã hold thì sẵn trong kho.
   - **Shortfall mọi level** (gom theo product): procurement tới `WH/Stock` → Odoo tự chọn Manufacture hay Buy, 1/2/3-step, vendor, lead time… theo rule set ở Inventory → **MO / FG-** nhập kho, PO mua RAW.
4. `stock.move` có `plan_order_id` ở state confirmed → `_action_assign()` ngay trong cùng transaction → hold cũ chuyển thành reservation thật của OUT / MO raw.
5. Hook `stock.move._action_done`: mỗi khi move của plan done (MO xong, PO nhận) → auto `_action_assign()` các move còn chờ của cùng plan & product (OUT giao khách, MO raw cấp trên).

### Yêu cầu cấu hình Inventory để netting đúng
- FG & component **không** dùng route MTO (`Replenish on Order`). Module tự netting; nếu bật MTO Odoo sẽ tạo MO cho full qty (v14 `mts_else_mto` không split partial) → double.
- Product route: `Manufacture` hoặc `Buy` (đặt trên product / category như bình thường). Bước 1/2/3, sub-contract, vendor pricelist: core.

## 6. Bảo toàn dữ liệu / edge cases
- Nhả hold **không** tham chiếu `quant_id` (cron `_merge_quants` có thể gộp/xoá quant) mà theo (product, location, lot, package, owner, strict=True), cap ở `reserved_quantity` hiện có → không bao giờ raise "unreserve more than reserved".
- Reserve luôn tính `_get_available_quantity()` trước → không raise "reserve more than available".
- `plan.order.line` unique theo sale_line/claim_line/demand_line còn active; qty mặc định = remaining.
- Xoá plan / reset draft → nhả hết hold.
- Thứ tự ưu tiên: sequence trên line (kéo thả) — line trên lấy hàng trước.

## 7. Đã kiểm chứng trên Odoo 14.0 CE (source, Python 3.9, PostgreSQL 16)
- Cài module sạch trên DB mới: OK. Tất cả view load qua web client: OK.
- Unit test `tests/test_plan_order.py` — 4/4 pass:
  1. SO confirm không sinh OUT/MO (gate).
  2. FG có 2, WHITE có 5, SO 10 FG → hold L0 FG=2, L1 WHITE=5, RAW=0; free_qty về 0; Release/Hold lại; Run → OUT 10 FG (reserve 2, gắn sale_line), MO FG=8 (raw 16 WHITE reserve 5), MO WHITE=11, PO RAW=33.
  3. Demand forecast 5 FG → không OUT, MO FG=3, demand → planned.
  4. Claim replace 1 FG need_delivery → OUT 1 tới khách, không MO, claim → planned.
  5. Sửa Quantity hold 5→3→4→10(lỗi)→0: available trên quant đúng theo từng bước, Run với hold 0 → MO 5.
  6. Cascade: FG hold 2/WHITE 16 → FG=0 → WHITE 20; Hold FG lại → WHITE 16; FG=1 → WHITE 18; Run → MO FG 9, raw reserve 18, không MO WHITE.
- Chưa test: MO 2/3-step (pbm/sam), multi-company, lot tracking. Test 1 vòng trên DB thật.

## 8. Roadmap gợi ý
- v1 (bản này): plan, reserve, run, trace.
- v2: report "Plan requirement" (pivot product × level: need / held / short / on order), gantt theo date_planned, ưu tiên plan (priority) khi hold.
- v3: MPS-style rolling forecast, tự cancel/giảm MO khi SO giảm qty.
