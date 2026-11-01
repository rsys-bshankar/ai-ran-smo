// routes.go lists every platform route the SDK calls, as data: the OpenAPI document, the gateway prefix, the method and
// the path template of each. namespaces.go and auth.go call routes by these values and never spell a path.
//
// routes_test.go checks allRoutes against the committed docs/openapi/*.json, so a route renamed or removed in the platform
// fails this module's tests. Adding a helper for a new route means adding it here and to allRoutes, or that check does not
// see it.

package smosdk

import (
	"net/url"
	"strings"
)

// route is one platform route the SDK calls: the OpenAPI document it is in (docs/openapi/<spec>.json), the prefix
// R1 Termination serves it under ("" for a route the SDK reaches without the gateway's routing table: /bootstrap
// and SME's own token endpoints), the method, and the path template as the document writes it.
//
// routes_test.go checks every entry against the committed documents, so a route that is renamed or removed in the
// platform fails this module's build instead of an rApp at run time.
type route struct {
	spec, prefix, method, tmpl string
}

// path fills the template's {placeholders} in order with args (path-escaped).
func (r route) path(args ...string) string {
	out := r.prefix + r.tmpl
	for _, a := range args {
		i := strings.IndexByte(out, '{')
		j := strings.IndexByte(out, '}')
		if i < 0 || j < i {
			panic("smosdk: too many path arguments for " + r.tmpl)
		}
		out = out[:i] + url.PathEscape(a) + out[j+1:]
	}
	if strings.Contains(out, "{") {
		panic("smosdk: too few path arguments for " + r.tmpl)
	}
	return out
}

// gw builds a route served through the gateway: the prefix is the module's name, "/" + spec, as R1 Termination's routing
// table serves it.
func gw(spec, method, tmpl string) route {
	return route{spec: spec, prefix: "/" + spec, method: method, tmpl: tmpl}
}

var (
	// reached directly, not through the routing table
	bootstrapRoute           = route{spec: "r1-termination", method: "GET", tmpl: "/bootstrap"}
	invokerRegistrationRoute = route{spec: "sme", method: "POST", tmpl: "/invoker-registrations"}
	tokenRoute               = route{spec: "sme", method: "POST", tmpl: "/oauth2/token"}

	// data (DME)
	dmeTypesRoute          = gw("dme", "GET", "/dme-types")
	dmeProducersRoute      = gw("dme", "GET", "/production-capabilities")
	dmeProducerStatusRoute = gw("dme", "GET", "/production-capabilities/{producer_id}/status")
	dmeActionsRoute        = gw("dme", "POST", "/actions")

	// models (AIMgF's registry)
	aimgfModelsRoute = gw("aimgf", "GET", "/models")

	// platform (SME)
	smeRegisterProviderRoute   = gw("sme", "POST", "/provider-registrations")
	smeDeregisterProviderRoute = gw("sme", "DELETE", "/provider-registrations/{apf_id}")
	smeDiscoverRoute           = gw("sme", "GET", "/service-apis/v1/allServiceAPIs")

	// the rApp's own instance (rApp Management)
	instanceRoute          = gw("rapp-mgmt", "GET", "/instances/{instance_id}")
	operatorAPIGetRoute    = gw("rapp-mgmt", "GET", "/instances/{instance_id}/operator-api")
	operatorAPIPutRoute    = gw("rapp-mgmt", "PUT", "/instances/{instance_id}/operator-api")
	operatorAPIDeleteRoute = gw("rapp-mgmt", "DELETE", "/instances/{instance_id}/operator-api")
	bootstrapCompleteRoute = gw("rapp-mgmt", "POST", "/instances/{instance_id}/bootstrap-complete")
	performanceRoute       = gw("rapp-mgmt", "POST", "/instances/{instance_id}/performance")
)

// allRoutes lists every route above, for the check against docs/openapi.
var allRoutes = []route{
	bootstrapRoute, invokerRegistrationRoute, tokenRoute,
	dmeTypesRoute, dmeProducersRoute, dmeProducerStatusRoute, dmeActionsRoute,
	aimgfModelsRoute,
	smeRegisterProviderRoute, smeDeregisterProviderRoute, smeDiscoverRoute,
	instanceRoute, operatorAPIGetRoute, operatorAPIPutRoute, operatorAPIDeleteRoute, bootstrapCompleteRoute, performanceRoute,
}
