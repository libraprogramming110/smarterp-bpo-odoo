{
    'name': 'SmartERP for BPO',
    'version': '20.0.1.0.0',
    'summary': 'AI features for BPO on Odoo CRM',
    'author': 'ISY-131 Team',
    'category': 'Sales/CRM',
    'depends': ['base', 'crm', 'contacts', 'hr'],
    'data': [
        'data/utm_source_data.xml',
        'views/placeholders.xml',
        'views/crm_lead_views.xml',
        'views/menus.xml',
        'views/res_partner_views.xml',
        'views/hr_employee_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'smarterp_bpo/static/src/placeholder/*',
        ],
    },
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
