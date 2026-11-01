// namespaces_test.go covers the typed helpers of namespaces.go and the Page and Object types: for each helper the
// method, path, query and body that reach the server, and how the answer is decoded. It uses fakeStack (stack_test.go)
// and the record helper below. Run: cd smo/sdk-go && go test -race ./... .

package smosdk

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"reflect"
	"testing"
)

// TestPageAcceptsTheEnvelopeAndABareArray
// pins that Page decodes the list envelope with its counts, an envelope without total, a bare array (the CAPIF routes)
// and rejects anything else.
func TestPageAcceptsTheEnvelopeAndABareArray(t *testing.T) {
	var p Page[Object]
	if err := json.Unmarshal([]byte(`{"items":[{"a":1},{"a":2}],"total":7,"limit":2,"offset":4}`), &p); err != nil {
		t.Fatal(err)
	}
	if len(p.Items) != 2 || p.Total == nil || *p.Total != 7 || p.Limit != 2 || p.Offset != 4 || p.HasMore {
		t.Fatalf("%+v", p)
	}
	if err := json.Unmarshal([]byte(`{"items":[],"limit":100,"offset":0,"hasMore":true}`), &p); err != nil {
		t.Fatal(err)
	}
	if p.Total != nil || !p.HasMore || len(p.Items) != 0 {
		t.Fatalf("?total=false envelope: %+v", p)
	}
	if err := json.Unmarshal([]byte(` [{"a":1}]`), &p); err != nil || len(p.Items) != 1 || p.Total != nil {
		t.Fatalf("bare array: %v %+v", err, p)
	}
	if err := json.Unmarshal([]byte(`"x"`), &p); err == nil {
		t.Fatal("expected an error")
	}
}

// TestObjectAs
// pins that ObjectAs decodes an Object into a struct.
func TestObjectAs(t *testing.T) {
	var v struct {
		State string `json:"state"`
	}
	if err := ObjectAs(Object{"state": "RUNNING"}, &v); err != nil || v.State != "RUNNING" {
		t.Fatalf("%v %+v", err, v)
	}
}

// seen is what the fake API received for one call.
type seen struct {
	method, path, query, body string
}

// record makes the API answer with `answer` and records what it was sent.
func record(s *fakeStack, status int, answer any, into *seen) {
	s.api = func(w http.ResponseWriter, r *http.Request) {
		*into = seen{r.Method, r.URL.Path, r.URL.RawQuery, readBody(r)}
		if answer == nil {
			w.WriteHeader(status)
			return
		}
		writeJSON(w, status, answer)
	}
}

// TestDataNamespace
// pins the routes and query of the DME helpers, including that an empty data category sends no query.
func TestDataNamespace(t *testing.T) {
	s := newStack(t)
	var got seen
	c := s.client(t)
	ctx := context.Background()

	record(s, 200, map[string]any{"items": []any{map[string]any{"typeName": "PRB"}}, "total": 1, "limit": 100, "offset": 0}, &got)
	p, err := c.Data().DiscoverTypes(ctx, "PM")
	if err != nil || got != (seen{"GET", "/dme/dme-types", "data_category=PM", ""}) || p.Items[0]["typeName"] != "PRB" {
		t.Fatalf("err=%v seen=%+v page=%+v", err, got, p)
	}
	if _, err = c.Data().DiscoverTypes(ctx, ""); err != nil || got.query != "" {
		t.Fatalf("no category sends no query: %v %+v", err, got)
	}
	if _, err = c.Data().ListProducers(ctx); err != nil || got.path != "/dme/production-capabilities" {
		t.Fatalf("%v %+v", err, got)
	}
	record(s, 200, map[string]any{"status": "ENABLED"}, &got)
	o, err := c.Data().QueryProducerStatus(ctx, "p/1")
	if err != nil || got.path != "/dme/production-capabilities/p%2F1/status" && got.path != "/dme/production-capabilities/p/1/status" || o["status"] != "ENABLED" {
		t.Fatalf("%v %+v %v", err, got, o)
	}
}

// TestModelsNamespace
// pins that Models().List sends the caller's query to the registry route.
func TestModelsNamespace(t *testing.T) {
	s := newStack(t)
	var got seen
	record(s, 200, []any{map[string]any{"id": "m1"}}, &got)
	p, err := s.client(t).Models().List(context.Background(), url.Values{"limit": {"10"}})
	if err != nil || got.path != "/aimgf/models" || got.query != "limit=10" || len(p.Items) != 1 {
		t.Fatalf("%v %+v %+v", err, got, p)
	}
}

// TestPlatformNamespace
// pins the SME helpers: provider registration (a null providerDomainInfo when empty), deregistration, and service
// discovery sending only the filter fields that are set.
func TestPlatformNamespace(t *testing.T) {
	s := newStack(t)
	var got seen
	c := s.client(t)
	ctx := context.Background()

	record(s, 201, map[string]any{"apfId": "rapp-1"}, &got)
	if _, err := c.Platform().RegisterProvider(ctx, "rapp-1", ""); err != nil {
		t.Fatal(err)
	}
	if got.method != "POST" || got.path != "/sme/provider-registrations" || got.body != `{"apfId":"rapp-1","providerDomainInfo":null}` {
		t.Fatalf("%+v", got)
	}
	if _, err := c.Platform().RegisterProvider(ctx, "rapp-1", "dom"); err != nil || got.body != `{"apfId":"rapp-1","providerDomainInfo":"dom"}` {
		t.Fatalf("%v %+v", err, got)
	}
	record(s, 204, nil, &got)
	if err := c.Platform().DeregisterProvider(ctx, "rapp-1"); err != nil || got.method != "DELETE" || got.path != "/sme/provider-registrations/rapp-1" {
		t.Fatalf("%v %+v", err, got)
	}
	record(s, 200, []any{}, &got)
	if _, err := c.Platform().DiscoverServices(ctx, ServiceFilter{APIName: "dme", Protocol: "HTTP_1_1"}); err != nil ||
		got.path != "/sme/service-apis/v1/allServiceAPIs" || got.query != "api_name=dme&protocol=HTTP_1_1" {
		t.Fatalf("%v %+v", err, got)
	}
}

// TestRAppNamespace
// pins the rApp-Management helpers: the paths under the instance, a null operator-API base read as "", and the metrics
// object sent unwrapped as the body.
func TestRAppNamespace(t *testing.T) {
	s := newStack(t)
	var got seen
	c := s.client(t)
	ctx := context.Background()
	const id = "7b0d6c1e-0000-4000-8000-000000000001"
	base := "/rapp-mgmt/instances/" + id

	record(s, 200, map[string]any{"instanceId": id, "state": "RUNNING"}, &got)
	if o, err := c.RApp().Instance(ctx, id); err != nil || got.path != base || o["state"] != "RUNNING" {
		t.Fatalf("%v %+v", err, got)
	}

	record(s, 200, map[string]any{"instanceId": id, "state": "RUNNING", "operatorApiBase": "http://rapp:8000"}, &got)
	if b, st, err := c.RApp().OperatorAPI(ctx, id); err != nil || b != "http://rapp:8000" || st != "RUNNING" || got.path != base+"/operator-api" {
		t.Fatalf("%v %q %q %+v", err, b, st, got)
	}
	record(s, 200, map[string]any{"instanceId": id, "state": "DEPLOYING", "operatorApiBase": nil}, &got)
	if b, _, err := c.RApp().OperatorAPI(ctx, id); err != nil || b != "" {
		t.Fatalf("a null base is %q, %v", b, err)
	}

	record(s, 200, map[string]any{}, &got)
	if err := c.RApp().RegisterOperatorAPI(ctx, id, "http://rapp:8000"); err != nil ||
		got.method != "PUT" || got.path != base+"/operator-api" || got.body != `{"operatorApiBase":"http://rapp:8000"}` {
		t.Fatalf("%v %+v", err, got)
	}
	record(s, 204, nil, &got)
	if err := c.RApp().ClearOperatorAPI(ctx, id); err != nil || got.method != "DELETE" || got.path != base+"/operator-api" {
		t.Fatalf("%v %+v", err, got)
	}

	record(s, 200, map[string]any{"state": "RUNNING"}, &got)
	if _, err := c.RApp().BootstrapComplete(ctx, id); err != nil || got.method != "POST" || got.path != base+"/bootstrap-complete" {
		t.Fatalf("%v %+v", err, got)
	}
	// the metrics object is the body itself, not wrapped
	if _, err := c.RApp().ReportPerformance(ctx, id, map[string]any{"cpu": 0.5}); err != nil || got.path != base+"/performance" || got.body != `{"cpu":0.5}` {
		t.Fatalf("%v %+v", err, got)
	}
}

// TestARolePolicyRefusalIsAForbiddenError
// pins that the gateway's 403 ROLE_NOT_PERMITTED arrives as *Error with that title and satisfies IsForbidden.
func TestARolePolicyRefusalIsAForbiddenError(t *testing.T) {
	s := newStack(t)
	s.api = func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 403, map[string]any{"detail": map[string]any{"title": "ROLE_NOT_PERMITTED", "status": 403, "detail": "an rApp may not change this route"}})
	}
	_, err := s.client(t).RApp().BootstrapComplete(context.Background(), "i1")
	if !IsForbidden(err) || err.(*Error).Title != "ROLE_NOT_PERMITTED" {
		t.Fatalf("err = %v", err)
	}
	_ = reflect.TypeOf(err)
}
