// routes_test.go checks routes.go: every route the SDK calls exists, with its method, in the committed
// docs/openapi/<module>.json, and route.path fills and escapes its placeholders. It reads those documents from the
// repository (../docs/openapi), so run it inside a checkout of smo/: cd smo/sdk-go && go test ./... .

package smosdk

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestEveryRouteTheSDKCallsIsInTheCommittedOpenAPIDocuments is the drift check that stands in for generated clients
// (README, "Why hand-written"): docs/openapi/<module>.json is generated from each module's live FastAPI schema and
// kept current by the platform's own CI, so a route renamed or removed there fails this test instead of an rApp.
func TestEveryRouteTheSDKCallsIsInTheCommittedOpenAPIDocuments(t *testing.T) {
	// R1 prefixes that R1 Termination's routing table sends to another module's process, whose document then holds the path: since S2.1 / S2.2
	// the model registry and the deploy gate are AIMgF's (/mlmr, /mllf), until S2.4 moves these routes to /aimgf.
	servedBy := map[string]string{"mlmr": "aimgf", "mllf": "aimgf"}
	docs := map[string]map[string]map[string]json.RawMessage{}
	for _, r := range allRoutes {
		if _, ok := docs[r.spec]; !ok {
			docName := r.spec
			if m, ok := servedBy[r.spec]; ok {
				docName = m
			}
			raw, err := os.ReadFile(filepath.Join("..", "docs", "openapi", docName+".json"))
			if err != nil {
				t.Fatalf("%s: %v", r.spec, err)
			}
			var doc struct {
				Paths map[string]map[string]json.RawMessage `json:"paths"`
			}
			if err := json.Unmarshal(raw, &doc); err != nil {
				t.Fatalf("%s: %v", r.spec, err)
			}
			docs[r.spec] = doc.Paths
		}
		ops, ok := docs[r.spec][r.tmpl]
		if !ok {
			t.Errorf("%s %s is not a path of the document R1 sends /%s to", r.method, r.tmpl, r.spec)
			continue
		}
		if _, ok := ops[strings.ToLower(r.method)]; !ok {
			t.Errorf("%s %s: the document has the path but not the method", r.method, r.tmpl)
		}
	}
}

// TestRoutePathFillsAndEscapesPlaceholders
// pins that arguments are path-escaped into the template in order, and that too many or too few arguments panic, as a
// programming error.
func TestRoutePathFillsAndEscapesPlaceholders(t *testing.T) {
	if got := operatorAPIPutRoute.path("a/b c"); got != "/rapp-mgmt/instances/a%2Fb%20c/operator-api" {
		t.Fatalf("path = %q", got)
	}
	if got := tokenRoute.path(); got != "/oauth2/token" {
		t.Fatalf("direct route path = %q", got)
	}
	for name, f := range map[string]func(){
		"too many": func() { dmeTypesRoute.path("x") },
		"too few":  func() { instanceRoute.path() },
	} {
		func() {
			defer func() {
				if recover() == nil {
					t.Errorf("%s: expected a panic (a programming error)", name)
				}
			}()
			f()
		}()
	}
}
