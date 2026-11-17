/** @odoo-module **/
// Placeholder page for a planned SmartERP feature. Delete with the feature's
// ir.actions.client record once the real feature ships.
import { Component, useProps } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";

export class SmartErpPlaceholder extends Component {
    static template = "smarterp_bpo.Placeholder";

    props = useProps(standardActionServiceProps);

    get info() {
        const action = this.props.action || {};
        return { name: action.name, ...(action.params || {}) };
    }
}

registry.category("actions").add("smarterp_bpo.placeholder", SmartErpPlaceholder);
