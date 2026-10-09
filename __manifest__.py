# -*- coding: utf-8 -*-
# Odoo 20.0
{
    'name': 'Production Planning - Plan Order (BOM-level stock hold)',
    'version': '20.0.1.0.0',
    'category': 'Manufacturing/Manufacturing',
    'summary': 'Group Sale Orders, forecast demand and customer claims into a Plan Order, '
               'hold stock per BOM level, then run procurement for the net shortfall only',
    'author': 'District Eight',
    'website': 'https://www.districteight.com',
    'support': 'manh.nv@districteight.com',
    'license': 'LGPL-3',
    'depends': ['sale_stock', 'mrp', 'purchase_stock'],
    'data': [
        'security/security.xml',
        'security/ir.access.csv',
        'data/sequence_data.xml',
        'wizard/plan_add_line_wizard_views.xml',
        'views/plan_order_views.xml',
        'views/plan_demand_views.xml',
        'views/plan_claim_views.xml',
        'views/sale_order_views.xml',
        'views/res_config_settings_views.xml',
        'views/menu_views.xml',
    ],
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': True,
    'auto_install': False,
}
