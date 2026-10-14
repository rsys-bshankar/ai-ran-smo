// msac_credential_test.go covers WithMSACCredential of DataClient.MediateAction (SEC-15.6b): the credential of an MSAC Identity
// goes in the X-MSAC-Credential header of POST /dme/actions only when given, never in the body, an error or any printed form of
// the client, it survives the SDK's repeats next to the Idempotency-Key, and the header the SDK sends is the one the committed
// DME OpenAPI document declares. It uses fakeStack (stack_test.go). Run: cd smo/sdk-go && go test -race ./... .

package smosdk

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

const secretCredential = "s3cr3t-msac-credential-value"

type actionSeen struct {
	method, path, body string
	header             http.Header
}

// recordActions makes the API answer each request with the next status of statuses (the last repeats) and records every request.
func recordActions(s *fakeStack, statuses ...int) *[]actionSeen {
	var mu sync.Mutex
	var got []actionSeen
	s.api = func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		n := len(got)
		got = append(got, actionSeen{r.Method, r.URL.Path, readBody(r), r.Header.Clone()})
		mu.Unlock()
		status := statuses[len(statuses)-1]
		if n < len(statuses) {
			status = statuses[n]
		}
		switch status {
		case 409:
			writeJSON(w, 409, map[string]any{"detail": map[string]any{"title": "CONCURRENT_MODIFICATION", "status": 409}})
		case 403:
			writeJSON(w, 403, map[string]any{"detail": map[string]any{"title": "MSAC_ACCESS_DENIED", "status": 403, "detail": "the credential is missing or wrong"}})
		case 503:
			writeJSON(w, 503, map[string]any{"detail": "busy"})
		default:
			writeJSON(w, status, map[string]any{"status": "FORWARDED"})
		}
	}
	return &got
}

var someAction = ActionRequest{RequestedBy: "ident-1", Changes: []map[string]any{{"managedElementRef": "me-1"}}}

// TestMediateActionSendsTheCredentialHeaderOnlyWhenGiven
// pins that the header is sent with the value when the option is used, is absent without it, and that the route is POST /dme/actions.
func TestMediateActionSendsTheCredentialHeaderOnlyWhenGiven(t *testing.T) {
	s := newStack(t)
	got := recordActions(s, 200)
	c := s.client(t)
	if _, err := c.Data().MediateAction(context.Background(), someAction, WithMSACCredential(secretCredential)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Data().MediateAction(context.Background(), someAction); err != nil {
		t.Fatal(err)
	}
	if len(*got) != 2 || (*got)[0].method != "POST" || (*got)[0].path != "/dme/actions" {
		t.Fatalf("seen = %+v", *got)
	}
	if v := (*got)[0].header.Get(MSACCredentialHeader); v != secretCredential {
		t.Fatalf("header = %q, want the credential", v)
	}
	if _, present := (*got)[1].header[http.CanonicalHeaderKey(MSACCredentialHeader)]; present {
		t.Fatalf("a call without the option carries the header: %v", (*got)[1].header)
	}
}

// TestTheCredentialHeaderNameIsTheOneTheDMEContractDeclares
// pins MSACCredentialHeader against docs/openapi/dme.json: POST /actions declares a header parameter of that name.
func TestTheCredentialHeaderNameIsTheOneTheDMEContractDeclares(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "docs", "openapi", "dme.json"))
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Paths map[string]map[string]struct {
			Parameters []struct{ Name, In string } `json:"parameters"`
		} `json:"paths"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, p := range doc.Paths[dmeActionsRoute.tmpl][strings.ToLower(dmeActionsRoute.method)].Parameters {
		found = found || (p.In == "header" && p.Name == MSACCredentialHeader)
	}
	if !found {
		t.Fatalf("docs/openapi/dme.json does not declare the header %s on POST %s", MSACCredentialHeader, dmeActionsRoute.tmpl)
	}
}

// TestTheCredentialIsNotInTheBody
// pins that the JSON body has the action's fields, unset optional fields are left out, and the credential is nowhere in it.
func TestTheCredentialIsNotInTheBody(t *testing.T) {
	s := newStack(t)
	got := recordActions(s, 200)
	req := ActionRequest{RequestedBy: "ident-1", Changes: []map[string]any{{"managedElementRef": "me-1"}}, MSACRole: "role-a", Decision: map[string]any{"rationale": "x"}}
	if _, err := s.client(t).Data().MediateAction(context.Background(), req, WithMSACCredential(secretCredential)); err != nil {
		t.Fatal(err)
	}
	body := (*got)[0].body
	if strings.Contains(body, secretCredential) || strings.Contains(strings.ToLower(body), "credential") {
		t.Fatalf("body = %s", body)
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(body), &m); err != nil {
		t.Fatal(err)
	}
	if m["requestedBy"] != "ident-1" || m["msacRole"] != "role-a" || m["decision"] == nil || len(m["changes"].([]any)) != 1 {
		t.Fatalf("body = %s", body)
	}
	if _, ok := m["scope"]; ok {
		t.Fatalf("an unset scope is sent: %s", body)
	}
	if _, ok := m["sourceContext"]; ok {
		t.Fatalf("an unset sourceContext is sent: %s", body)
	}
}

// TestTheCredentialIsNotInAnErrorOrAPrintedForm
// pins that a refusal's error text, and every %v, %+v, %#v and %s of the error, the client, the option and the request, lack the credential.
func TestTheCredentialIsNotInAnErrorOrAPrintedForm(t *testing.T) {
	s := newStack(t)
	recordActions(s, 403)
	c := s.client(t)
	opt := WithMSACCredential(secretCredential)
	_, err := c.Data().MediateAction(context.Background(), someAction, opt)
	if StatusOf(err) != 403 {
		t.Fatalf("err = %v, want the 403", err)
	}
	for _, v := range []any{err, c, opt, someAction, c.Data()} {
		for _, verb := range []string{"%v", "%+v", "%#v", "%s"} {
			if out := fmt.Sprintf(verb, v); strings.Contains(out, secretCredential) {
				t.Errorf("%s of %T contains the credential: %s", verb, v, out)
			}
		}
	}
}

// TestTheCredentialIsKeptNextToTheIdempotencyKeyOnARepeat
// pins that the repeat after a 503 and the repeat after CONCURRENT_MODIFICATION carry the same credential and the same Idempotency-Key as the first try.
func TestTheCredentialIsKeptNextToTheIdempotencyKeyOnARepeat(t *testing.T) {
	for name, first := range map[string]int{"503": 503, "CONCURRENT_MODIFICATION": 409} {
		s := newStack(t)
		got := recordActions(s, first, 200)
		if _, err := s.client(t).Data().MediateAction(context.Background(), someAction, WithMSACCredential(secretCredential)); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if len(*got) != 2 {
			t.Fatalf("%s: %d attempts, want 2", name, len(*got))
		}
		k0, k1 := (*got)[0].header.Get("Idempotency-Key"), (*got)[1].header.Get("Idempotency-Key")
		if k0 == "" || k0 != k1 {
			t.Fatalf("%s: keys %q %q", name, k0, k1)
		}
		for i, g := range *got {
			if g.header.Get(MSACCredentialHeader) != secretCredential {
				t.Fatalf("%s: attempt %d lost the credential: %v", name, i, g.header)
			}
		}
	}
}

// TestTheSDKReadsNoEnvironmentVariableForTheCredential
// pins that without the option no header is sent even when a plausible variable is set.
func TestTheSDKReadsNoEnvironmentVariableForTheCredential(t *testing.T) {
	for _, name := range []string{"MSAC_CREDENTIAL", "SMO_MSAC_CREDENTIAL", "X_MSAC_CREDENTIAL"} {
		t.Setenv(name, secretCredential)
	}
	s := newStack(t)
	got := recordActions(s, 200)
	if _, err := s.client(t).Data().MediateAction(context.Background(), someAction); err != nil {
		t.Fatal(err)
	}
	if _, present := (*got)[0].header[http.CanonicalHeaderKey(MSACCredentialHeader)]; present {
		t.Fatalf("header sent from the environment: %v", (*got)[0].header)
	}
}
