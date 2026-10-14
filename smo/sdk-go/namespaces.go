// namespaces.go holds the typed helpers an rApp starts with: Data (DME), Models (MLMR), Platform (SME) and RApp (the
// instance's own routes at rApp Management), plus Object and Page, the shapes they return. Each method is one route of
// routes.go sent through Client.Do, so the token, retries and error mapping apply.
//
// The helpers add no validation and do not re-model the platform's schemas (golden rule 6): an answer is an Object, and
// the platform's own error comes back as *Error. A route without a helper is one Client.Do call.

package smosdk

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/url"
)

// Object is a JSON object as the platform returned it. The SDK does not re-model the platform's schemas (golden rule 6:
// no client-side re-validation, the platform owns the shapes); decode into your own struct with ObjectAs when you want types.
type Object map[string]any

// ObjectAs decodes an Object into v (a pointer to a struct) through JSON.
func ObjectAs(o Object, v any) error {
	b, err := json.Marshal(o)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, v)
}

// Page is the platform's list envelope {items, total, limit, offset} (smo_shared/pagination.py). Unlike the Python SDK,
// which unwraps it and drops the counts, Go keeps them, so a caller can see that a list was cut at the route's default
// limit. A route that answers a bare JSON array (the CAPIF discovery routes) fills Items only.
type Page[T any] struct {
	Items   []T
	Total   *int // nil when the request asked ?total=false or the route has no count
	Limit   int
	Offset  int
	HasMore bool
}

// UnmarshalJSON accepts the envelope or a bare array.
func (p *Page[T]) UnmarshalJSON(b []byte) error {
	if t := bytes.TrimSpace(b); len(t) > 0 && t[0] == '[' {
		return json.Unmarshal(t, &p.Items)
	}
	var env struct {
		Items   []T  `json:"items"`
		Total   *int `json:"total"`
		Limit   int  `json:"limit"`
		Offset  int  `json:"offset"`
		HasMore bool `json:"hasMore"`
	}
	if err := json.Unmarshal(b, &env); err != nil {
		return err
	}
	*p = Page[T]{Items: env.Items, Total: env.Total, Limit: env.Limit, Offset: env.Offset, HasMore: env.HasMore}
	return nil
}

// call sends one route of routes.go with its path arguments, query and JSON body, and decodes the answer into out.
func (c *Client) call(ctx context.Context, rt route, args []string, q url.Values, body, out any) error {
	return c.callWith(ctx, rt, args, q, body, nil, out)
}

// callWith is call with extra request headers (nil: none).
func (c *Client) callWith(ctx context.Context, rt route, args []string, q url.Values, body any, hdr http.Header, out any) error {
	return c.Do(ctx, Request{Method: rt.method, Path: rt.path(args...), Query: q, Body: body, Header: hdr}, out)
}

// setIf sets a query parameter only when its value is not empty, so an unset filter is not sent.
func setIf(q url.Values, key, value string) {
	if value != "" {
		q.Set(key, value)
	}
}

// ---------------------------------------------------------------- data (DME)

// DataClient is sdk.data: DME, R1AP clause 7. The reads an rApp starts with and MediateAction; use Client.Do for the rest.
type DataClient struct{ c *Client }

// Data returns the data namespace.
func (c *Client) Data() DataClient { return DataClient{c} }

// DiscoverTypes lists the registered data types, optionally of one data category (GET /dme/dme-types).
func (d DataClient) DiscoverTypes(ctx context.Context, dataCategory string) (Page[Object], error) {
	q := url.Values{}
	setIf(q, "data_category", dataCategory)
	var p Page[Object]
	return p, d.c.call(ctx, dmeTypesRoute, nil, q, nil, &p)
}

// ListProducers lists the data producers (GET /dme/production-capabilities).
func (d DataClient) ListProducers(ctx context.Context) (Page[Object], error) {
	var p Page[Object]
	return p, d.c.call(ctx, dmeProducersRoute, nil, nil, nil, &p)
}

// QueryProducerStatus is GET /dme/production-capabilities/{producer_id}/status.
func (d DataClient) QueryProducerStatus(ctx context.Context, producerID string) (Object, error) {
	var o Object
	return o, d.c.call(ctx, dmeProducerStatusRoute, []string{producerID}, nil, nil, &o)
}

// MSACCredentialHeader is the header that carries the credential of an MSAC Identity (RAN NF OAM reads the same name, SEC-15.6).
const MSACCredentialHeader = "X-MSAC-Credential"

// ActionRequest is the body of MediateAction (DME's ActionRequest). RequestedBy and Changes are required; Scope defaults
// to "single-ME" at the platform when empty, and the other fields are left out of the body when empty.
type ActionRequest struct {
	RequestedBy   string           // the MSAC Identity (or Role) the action is made for
	Changes       []map[string]any // each: managedElementRef, managedFunctionRef, attributeChanges, operation, ...
	Scope         string
	MSACRole      string
	SourceContext map[string]any
	Decision      map[string]any // why the rApp acts (PR-AI-13): inputsRef, modelVersion, rationale
}

// ActionOption changes one MediateAction call.
type ActionOption func(*actionCall)

type actionCall struct{ credential *string }

// WithMSACCredential presents the credential of the MSAC Identity named by ActionRequest.RequestedBy (SEC-15.6b). RAN NF OAM
// asks an rApp for it when that Identity has one, and answers 403 MSAC_ACCESS_DENIED without it. The SDK sends it in the
// X-MSAC-Credential header of this call and nowhere else: not in the body, not in an error, not in a log (the SDK logs nothing)
// and not in any String of the SDK's types; it reads no environment variable for it. A call without this option sends no such
// header. Pass the secret in from wherever the rApp keeps secrets.
func WithMSACCredential(credential string) ActionOption {
	return func(a *actionCall) { a.credential = &credential }
}

// MediateAction submits an O1 configuration action to DME, which mediates it to RAN NF OAM (POST /dme/actions). It returns the
// action record. Like every POST it carries an Idempotency-Key, and a repeat carries the same one and the same options.
func (d DataClient) MediateAction(ctx context.Context, req ActionRequest, opts ...ActionOption) (Object, error) {
	var call actionCall
	for _, o := range opts {
		o(&call)
	}
	body := map[string]any{"requestedBy": req.RequestedBy, "changes": req.Changes}
	if req.Changes == nil {
		body["changes"] = []map[string]any{}
	}
	if req.Scope != "" {
		body["scope"] = req.Scope
	}
	if req.MSACRole != "" {
		body["msacRole"] = req.MSACRole
	}
	if req.SourceContext != nil {
		body["sourceContext"] = req.SourceContext
	}
	if req.Decision != nil {
		body["decision"] = req.Decision
	}
	var hdr http.Header
	if call.credential != nil {
		hdr = http.Header{MSACCredentialHeader: {*call.credential}}
	}
	var o Object
	return o, d.c.callWith(ctx, dmeActionsRoute, nil, nil, body, hdr, &o)
}

// ---------------------------------------------------------------- models (MLMR)

// ModelsClient is sdk.models: MLMR (TS 28.105 / TS 29.482 MLModelManagement). Only the list read.
type ModelsClient struct{ c *Client }

// Models returns the models namespace.
func (c *Client) Models() ModelsClient { return ModelsClient{c} }

// List is GET /mlmr/models; query carries the route's own filters and paging (limit, offset, ...).
func (m ModelsClient) List(ctx context.Context, query url.Values) (Page[Object], error) {
	var p Page[Object]
	return p, m.c.call(ctx, mlmrModelsRoute, nil, query, nil, &p)
}

// ---------------------------------------------------------------- platform (SME)

// PlatformClient is sdk.platform: SME, CAPIF (TS 29.222).
type PlatformClient struct{ c *Client }

// Platform returns the platform namespace.
func (c *Client) Platform() PlatformClient { return PlatformClient{c} }

// RegisterProvider is POST /sme/provider-registrations. providerDomainInfo may be empty.
func (p PlatformClient) RegisterProvider(ctx context.Context, apfID, providerDomainInfo string) (Object, error) {
	body := map[string]any{"apfId": apfID, "providerDomainInfo": nil}
	if providerDomainInfo != "" {
		body["providerDomainInfo"] = providerDomainInfo
	}
	var o Object
	return o, p.c.call(ctx, smeRegisterProviderRoute, nil, nil, body, &o)
}

// DeregisterProvider is DELETE /sme/provider-registrations/{apf_id}.
func (p PlatformClient) DeregisterProvider(ctx context.Context, apfID string) error {
	return p.c.call(ctx, smeDeregisterProviderRoute, []string{apfID}, nil, nil, nil)
}

// ServiceFilter narrows DiscoverServices; an empty field is not sent.
type ServiceFilter struct {
	APIInvokerID, APIName, APIVersion, AEFID, Protocol, DataFormat, CommType string
}

// DiscoverServices is GET /sme/service-apis/v1/allServiceAPIs.
func (p PlatformClient) DiscoverServices(ctx context.Context, f ServiceFilter) (Page[Object], error) {
	q := url.Values{}
	setIf(q, "api_invoker_id", f.APIInvokerID)
	setIf(q, "api_name", f.APIName)
	setIf(q, "api_version", f.APIVersion)
	setIf(q, "aef_id", f.AEFID)
	setIf(q, "protocol", f.Protocol)
	setIf(q, "data_format", f.DataFormat)
	setIf(q, "comm_type", f.CommType)
	var pg Page[Object]
	return pg, p.c.call(ctx, smeDiscoverRoute, nil, q, nil, &pg)
}

// ---------------------------------------------------------------- the rApp's own instance (rApp Management)

// RAppClient is the rApp's side of rApp Management (/rapp-mgmt/instances/{id}/...).
//
// Which of these an rApp may call through R1 is decided by the role policy (smo_shared/roles.py): reads are open and
// registering or clearing the operator API of the caller's own instance, BootstrapComplete and ReportPerformance are
// allowed for the caller's own instance (rApp Management answers 403 NOT_THIS_INSTANCE for another's). PR-SEC-10 put them
// on the rApp allow-list: the container reports that it is up and how it performs.
type RAppClient struct{ c *Client }

// RApp returns the rApp-instance namespace.
func (c *Client) RApp() RAppClient { return RAppClient{c} }

// Instance is GET /rapp-mgmt/instances/{instance_id}.
func (r RAppClient) Instance(ctx context.Context, instanceID string) (Object, error) {
	var o Object
	return o, r.c.call(ctx, instanceRoute, []string{instanceID}, nil, nil, &o)
}

// OperatorAPI is GET .../operator-api: the registered base URL ("" when none) and the instance state.
func (r RAppClient) OperatorAPI(ctx context.Context, instanceID string) (base, state string, err error) {
	var ans struct {
		State           string  `json:"state"`
		OperatorAPIBase *string `json:"operatorApiBase"`
	}
	if err = r.c.call(ctx, operatorAPIGetRoute, []string{instanceID}, nil, nil, &ans); err != nil {
		return "", "", err
	}
	if ans.OperatorAPIBase != nil {
		base = *ans.OperatorAPIBase
	}
	return base, ans.State, nil
}

// RegisterOperatorAPI is PUT .../operator-api: where R1 reaches this instance's operator API (the routes its
// manifest's operatorUi declares). Allowed for the instance itself (its invoker id is the instance's oauthClientId).
func (r RAppClient) RegisterOperatorAPI(ctx context.Context, instanceID, operatorAPIBase string) error {
	return r.c.call(ctx, operatorAPIPutRoute, []string{instanceID}, nil, map[string]string{"operatorApiBase": operatorAPIBase}, nil)
}

// ClearOperatorAPI is DELETE .../operator-api (idempotent).
func (r RAppClient) ClearOperatorAPI(ctx context.Context, instanceID string) error {
	return r.c.call(ctx, operatorAPIDeleteRoute, []string{instanceID}, nil, nil, nil)
}

// BootstrapComplete is POST .../bootstrap-complete: DEPLOYING to RUNNING. See the type's note on who may call it.
func (r RAppClient) BootstrapComplete(ctx context.Context, instanceID string) (Object, error) {
	var o Object
	return o, r.c.call(ctx, bootstrapCompleteRoute, []string{instanceID}, nil, nil, &o)
}

// ReportPerformance is POST .../performance with the metrics object as the body (the route takes it unwrapped).
// See the type's note on who may call it.
func (r RAppClient) ReportPerformance(ctx context.Context, instanceID string, metrics map[string]any) (Object, error) {
	var o Object
	return o, r.c.call(ctx, performanceRoute, []string{instanceID}, nil, metrics, &o)
}
