# -*- coding: utf-8 -*-
"""Small helpers hiding API differences between Odoo versions (14.0 -> 20.0)."""
from odoo import release

VERSION = release.version_info[0]


def is_storable(product):
    """Storable product: type == 'product' until 17.0, is_storable flag since 18.0."""
    if VERSION >= 18:
        return bool(product.is_storable)
    return product.type == 'product'


def storable_domain():
    if VERSION >= 18:
        return [('is_storable', '=', True)]
    return [('type', '=', 'product')]


def bom_find(env, product, company_id):
    """mrp.bom._bom_find: single record API in 14.0, batch (dict) API since 15.0."""
    Bom = env['mrp.bom']
    if VERSION >= 15:
        return Bom._bom_find(product, company_id=company_id, bom_type=False).get(product)
    return Bom._bom_find(product=product, company_id=company_id, bom_type=False)


def quant_reserve(env, product, location, qty):
    """Reserve `qty` (product UoM, already capped to the available quantity) on the quants under
    `location`. Returns [(quant, qty_reserved)].
    <= 16.0: stock.quant._update_reserved_quantity() picks the quants and returns the list.
    >= 17.0: stock.quant._get_reserve_quantity() picks, _update_reserved_quantity() writes one quant."""
    Quant = env['stock.quant']
    if VERSION >= 17:
        reserved = Quant._get_reserve_quantity(product, location, qty, strict=False)
        for quant, q in reserved:
            Quant._update_reserved_quantity(product, quant.location_id, q, lot_id=quant.lot_id,
                                            package_id=quant.package_id, owner_id=quant.owner_id, strict=True)
        return reserved
    return Quant._update_reserved_quantity(product, location, qty, strict=False)


def quant_unreserve(env, product, location, qty, lot=None, package=None, owner=None):
    """Release `qty` on the quant identified strictly by (product, location, lot, package, owner)."""
    env['stock.quant']._update_reserved_quantity(product, location, -qty, lot_id=lot, package_id=package,
                                                 owner_id=owner, strict=True)


# ---------------------------------------------------------------------------------------------
# Procurement plumbing: procurement.group (<= 18.0)  vs  stock.reference + stock.rule.run (>= 19.0)
# ---------------------------------------------------------------------------------------------
LOT_MODEL = 'stock.lot' if VERSION >= 16 else 'stock.production.lot'
PACKAGE_MODEL = 'stock.package' if VERSION >= 19 else 'stock.quant.package'
PROC_GROUP_MODEL = 'stock.reference' if VERSION >= 19 else 'procurement.group'


def procurement_runner(env):
    """Model exposing `.Procurement` and `.run(procurements)`."""
    return env['stock.rule'] if VERSION >= 19 else env['procurement.group']


def create_proc_group(env, name, partner=None):
    if VERSION >= 19:
        return env['stock.reference'].create({'name': name})
    vals = {'name': name, 'move_type': 'direct'}
    if partner:
        vals['partner_id'] = partner.id
    return env['procurement.group'].create(vals)


def group_values(group):
    """Procurement `values` entry linking the created documents to the group / reference."""
    if VERSION >= 19:
        return {'reference_ids': group}
    return {'group_id': group}


def sale_line_uom(sol):
    return sol.product_uom_id if VERSION >= 19 else sol.product_uom


def sale_line_procurement_values(sol):
    """Core procurement values of a sale order line, with its group / reference guaranteed."""
    if VERSION >= 19:
        if not sol.order_id.stock_reference_ids:
            sol.env['stock.reference'].create(sol._prepare_reference_vals())
        return sol._prepare_procurement_values()
    group_id = sol._get_procurement_group()
    if not group_id:
        group_id = sol.env['procurement.group'].create(sol._prepare_procurement_group_vals())
        sol.order_id.procurement_group_id = group_id
    return sol._prepare_procurement_values(group_id=group_id)


def sale_line_dest_location(sol):
    if VERSION >= 18:
        return sol._get_location_final()
    return sol.order_id.partner_shipping_id.property_stock_customer


def uom_rounding(uom):
    """uom.uom.rounding was removed in 20.0 (precision comes from 'Product Unit' decimal precision)."""
    if VERSION >= 20:
        digits = uom.env['decimal.precision'].precision_get('Product Unit')
        return 10 ** -digits
    return uom.rounding


def bom_uom(record):
    """mrp.bom / mrp.bom.line UoM field: product_uom_id until 19.0, uom_id since 20.0."""
    return record.uom_id if VERSION >= 20 else record.product_uom_id
