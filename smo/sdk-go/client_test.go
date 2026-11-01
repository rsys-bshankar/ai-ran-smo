// client_test.go covers Client.Do and roundTrip (client.go), RetryPolicy (retry.go) and New: retries and their limits,
// which calls may be repeated, the Idempotency-Key, the one repeat on CONCURRENT_MODIFICATION, context cancellation, the
// Retry-After header, headers and query, config validation and answer decoding. It uses fakeStack (stack_test.go) with a
// fast, jitter-free RetryPolicy. No network beyond httptest. Run: cd smo/sdk-go && go test -race ./... .

package smosdk

import (
	"context"
	"errors"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// TestRetriesAServerThatIsBusyThenSucceeds
// pins that 429, 502, 503 and 504 are repeated and the third attempt's success is returned.
func TestRetriesAServerThatIsBusyThenSucceeds(t *testing.T) {
	for _, status := range []int{429, 502, 503, 504} {
		s := newStack(t)
		var hits atomic.Int32
		s.api = func(w http.ResponseWriter, r *http.Request) {
			if hits.Add(1) < 3 {
				w.Header().Set("Retry-After", "0")
				writeJSON(w, status, map[string]any{"detail": "busy"})
				return
			}
			writeJSON(w, 200, map[string]any{"ok": true})
		}
		var out Object
		err := s.client(t).Do(context.Background(), Request{Method: "GET", Path: "/dme/dme-types"}, &out)
		if err != nil || out["ok"] != true || hits.Load() != 3 {
			t.Fatalf("status %d: err=%v out=%v hits=%d, want success on the third attempt", status, err, out, hits.Load())
		}
	}
}

// TestGivesUpAfterMaxAttemptsWithThePlatformsError
// pins that after the default four attempts the platform's own error (status, title, detail) is what the caller gets.
func TestGivesUpAfterMaxAttemptsWithThePlatformsError(t *testing.T) {
	s := newStack(t)
	var hits atomic.Int32
	s.api = func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		writeJSON(w, 503, map[string]any{"detail": map[string]any{"title": "DEPENDENCY_UNAVAILABLE", "status": 503, "detail": "aimgf is down"}})
	}
	err := get(t, s.client(t), "/aimgf/models")
	var e *Error
	if !errors.As(err, &e) || e.StatusCode != 503 || e.Title != "DEPENDENCY_UNAVAILABLE" || e.Detail != "aimgf is down" {
		t.Fatalf("err = %#v", err)
	}
	if hits.Load() != 4 {
		t.Fatalf("attempts = %d, want the default 4", hits.Load())
	}
}

// TestMaxAttemptsOneNeverRetries
// pins that MaxAttempts 1 means a single try.
func TestMaxAttemptsOneNeverRetries(t *testing.T) {
	s := newStack(t)
	var hits atomic.Int32
	s.api = func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		writeJSON(w, 503, map[string]any{"detail": "x"})
	}
	_ = get(t, s.client(t, func(c *Config) { c.Retry.MaxAttempts = 1 }), "/x")
	if hits.Load() != 1 {
		t.Fatalf("attempts = %d", hits.Load())
	}
}

// TestClientErrorsAreFinalAnswers
// pins that 400, 403, 404 and 422 are not repeated.
func TestClientErrorsAreFinalAnswers(t *testing.T) {
	for _, status := range []int{400, 403, 404, 422} {
		s := newStack(t)
		var hits atomic.Int32
		s.api = func(w http.ResponseWriter, r *http.Request) {
			hits.Add(1)
			writeJSON(w, status, map[string]any{"detail": "no"})
		}
		err := get(t, s.client(t), "/x")
		if StatusOf(err) != status || hits.Load() != 1 {
			t.Fatalf("status %d: err=%v attempts=%d", status, err, hits.Load())
		}
	}
}

// TestATransportErrorIsRetriedForASafeCall
// pins that a connection that drops without an answer is retried and the next answer returned.
func TestATransportErrorIsRetriedForASafeCall(t *testing.T) {
	s := newStack(t)
	var hits atomic.Int32
	s.api = func(w http.ResponseWriter, r *http.Request) {
		if hits.Add(1) == 1 {
			conn, _, _ := w.(http.Hijacker).Hijack()
			_ = conn.Close() // the connection drops without an answer
			return
		}
		writeJSON(w, 200, map[string]any{"ok": true})
	}
	if err := get(t, s.client(t), "/x"); err != nil || hits.Load() != 2 {
		t.Fatalf("err=%v hits=%d", err, hits.Load())
	}
}

// TestATransportErrorThatPersistsIsReturned
// pins that a transport failure is returned as the net error and not as a platform *Error, which would claim an answer existed.
func TestATransportErrorThatPersistsIsReturned(t *testing.T) {
	s := newStack(t)
	c := s.client(t)
	if err := get(t, c, "/x"); err != nil { // warm the token while the server is up
		t.Fatal(err)
	}
	s.srv.Close()
	err := get(t, c, "/x")
	var ne net.Error
	var e *Error
	if err == nil || errors.As(err, &e) || !errors.As(err, &ne) {
		t.Fatalf("err = %v (%T), want a net error, not a platform *Error", err, err)
	}
}

// TestAPostIsRepeatedWithTheSameIdempotencyKey
// pins that a POST is repeated after 502s and every repeat carries the same non-empty key, so the platform can answer
// from the first attempt instead of acting twice.
func TestAPostIsRepeatedWithTheSameIdempotencyKey(t *testing.T) {
	// the SDK puts an Idempotency-Key on every POST (generated once per call), so a POST is safe to repeat and the key is the same
	s := newStack(t)
	var keys []string
	s.api = func(w http.ResponseWriter, r *http.Request) {
		keys = append(keys, r.Header.Get("Idempotency-Key"))
		if len(keys) < 3 {
			writeJSON(w, 502, map[string]any{"detail": "bad gateway"})
			return
		}
		writeJSON(w, 201, map[string]any{"id": "1"})
	}
	var out Object
	err := s.client(t).Do(context.Background(), Request{Method: "POST", Path: "/dme/offers", Body: map[string]any{"a": 1}}, &out)
	if err != nil || len(keys) != 3 {
		t.Fatalf("err=%v keys=%v", err, keys)
	}
	if keys[0] == "" || keys[0] != keys[1] || keys[1] != keys[2] {
		t.Fatalf("keys = %v, want one non-empty key reused by every repeat", keys)
	}
}

// TestACallersOwnIdempotencyKeyWins
// pins that a key the caller supplies, in any header spelling, is sent instead of a generated one.
func TestACallersOwnIdempotencyKeyWins(t *testing.T) {
	s := newStack(t)
	s.api = func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Idempotency-Key") != "mine" {
			t.Errorf("key = %q", r.Header.Get("Idempotency-Key"))
		}
		writeJSON(w, 200, nil)
	}
	h := http.Header{"idempotency-key": {"mine"}}
	if err := s.client(t).Do(context.Background(), Request{Method: "POST", Path: "/x", Header: h}, nil); err != nil {
		t.Fatal(err)
	}
}

// TestOnlyPostsGetAnIdempotencyKey
// pins that GET, PUT, PATCH and DELETE carry no Idempotency-Key and POST does.
func TestOnlyPostsGetAnIdempotencyKey(t *testing.T) {
	s := newStack(t)
	s.api = func(w http.ResponseWriter, r *http.Request) {
		if (r.Header.Get("Idempotency-Key") != "") != (r.Method == "POST") {
			t.Errorf("%s carries Idempotency-Key %q", r.Method, r.Header.Get("Idempotency-Key"))
		}
		writeJSON(w, 200, nil)
	}
	c := s.client(t)
	for _, m := range []string{"GET", "PUT", "PATCH", "DELETE", "POST"} {
		if err := c.Do(context.Background(), Request{Method: m, Path: "/x"}, nil); err != nil {
			t.Fatal(err)
		}
	}
}

// TestEnrollmentIsNotRepeatedAfterAnAmbiguousFailure
// pins that a registration that got a 502 is not sent again, because each registration creates an invoker and the first may exist.
func TestEnrollmentIsNotRepeatedAfterAnAmbiguousFailure(t *testing.T) {
	// a registration mints a new invoker each time: after a 502 the first one may exist, so it is not sent again
	s := newStack(t)
	var regs atomic.Int32
	inner := s.srv.Config.Handler
	s.srv.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sme/invoker-registrations" {
			regs.Add(1)
			writeJSON(w, 502, map[string]any{"detail": "bad gateway"})
			return
		}
		inner.ServeHTTP(w, r)
	})
	err := get(t, s.client(t), "/x")
	if StatusOf(err) != 502 || regs.Load() != 1 {
		t.Fatalf("err=%v registrations=%d, want one registration and the 502", err, regs.Load())
	}
}

// TestEnrollmentIsRepeatedWhenTheServerSaysItDidNothing
// pins that a registration refused with 503 (the server did not act) is retried.
func TestEnrollmentIsRepeatedWhenTheServerSaysItDidNothing(t *testing.T) {
	s := newStack(t)
	var regs atomic.Int32
	inner := s.srv.Config.Handler
	s.srv.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sme/invoker-registrations" && regs.Add(1) == 1 {
			writeJSON(w, 503, map[string]any{"detail": "starting"})
			return
		}
		inner.ServeHTTP(w, r)
	})
	if err := get(t, s.client(t), "/x"); err != nil || regs.Load() != 2 {
		t.Fatalf("err=%v registrations=%d", err, regs.Load())
	}
}

// TestAConcurrentModificationIsRepeatedOnceWithTheSameKey
// pins the one repeat of a write that lost a race, with the same Idempotency-Key.
func TestAConcurrentModificationIsRepeatedOnceWithTheSameKey(t *testing.T) {
	s := newStack(t)
	var keys []string
	s.api = func(w http.ResponseWriter, r *http.Request) {
		keys = append(keys, r.Header.Get("Idempotency-Key"))
		if len(keys) == 1 {
			writeJSON(w, 409, map[string]any{"detail": map[string]any{"title": "CONCURRENT_MODIFICATION", "status": 409, "detail": "lost the race"}})
			return
		}
		writeJSON(w, 200, map[string]any{"ok": true})
	}
	if err := s.client(t).Do(context.Background(), Request{Method: "POST", Path: "/aimgf/models", Body: map[string]any{}}, nil); err != nil {
		t.Fatal(err)
	}
	if len(keys) != 2 || keys[0] != keys[1] {
		t.Fatalf("keys = %v", keys)
	}
}

// TestASecondConcurrentModificationIsReturned
// pins that the repeat happens once only: a second CONCURRENT_MODIFICATION is returned as a conflict.
func TestASecondConcurrentModificationIsReturned(t *testing.T) {
	s := newStack(t)
	var hits atomic.Int32
	s.api = func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		writeJSON(w, 409, map[string]any{"detail": map[string]any{"title": "CONCURRENT_MODIFICATION", "status": 409, "detail": "again"}})
	}
	err := s.client(t).Do(context.Background(), Request{Method: "PUT", Path: "/aimgf/models/1"}, nil)
	if !IsConflict(err) || hits.Load() != 2 {
		t.Fatalf("err=%v hits=%d", err, hits.Load())
	}
}

// TestOtherConflictsAndReadsAreNotRepeated
// pins that a 409 with another title, and a GET that gets CONCURRENT_MODIFICATION, are final after one try.
func TestOtherConflictsAndReadsAreNotRepeated(t *testing.T) {
	s := newStack(t)
	var hits atomic.Int32
	s.api = func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		title := "NAME_CONFLICT"
		if r.Method == "GET" {
			title = "CONCURRENT_MODIFICATION"
		}
		writeJSON(w, 409, map[string]any{"detail": map[string]any{"title": title, "status": 409, "detail": "x"}})
	}
	c := s.client(t)
	if err := c.Do(context.Background(), Request{Method: "POST", Path: "/x"}, nil); !IsConflict(err) {
		t.Fatal(err)
	}
	if err := get(t, c, "/x"); !IsConflict(err) {
		t.Fatal(err)
	}
	if hits.Load() != 2 {
		t.Fatalf("hits = %d, want 2 (one per call)", hits.Load())
	}
}

// TestAContextCancelledDuringBackoffReturnsPromptly
// pins that cancelling the context ends the wait between attempts at once with the context's error, not after the full delay.
func TestAContextCancelledDuringBackoffReturnsPromptly(t *testing.T) {
	s := newStack(t)
	s.api = func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 503, map[string]any{"detail": "x"}) }
	c := s.client(t, func(c *Config) { c.Retry = RetryPolicy{MaxAttempts: 5, BaseDelay: 10 * time.Second, NoJitter: true} })
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	start := time.Now()
	err := c.Do(ctx, Request{Method: "GET", Path: "/x"}, nil)
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(start) > 3*time.Second {
		t.Fatalf("err=%v after %v", err, time.Since(start))
	}
}

// TestAContextCancelledBeforeTheCallNeverReachesTheServer
// pins that an already cancelled context fails the call before any request, even the bootstrap one, is sent.
func TestAContextCancelledBeforeTheCallNeverReachesTheServer(t *testing.T) {
	s := newStack(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	err := s.client(t).Do(ctx, Request{Method: "GET", Path: "/x"}, nil)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v", err)
	}
	if b, _, _ := s.counts(); b != 0 {
		t.Fatal("the server was called")
	}
}

// TestRetryAfterRaisesTheWait
// pins that a Retry-After raises the wait above the backoff and is capped at maxRetryAfter.
func TestRetryAfterRaisesTheWait(t *testing.T) {
	p := RetryPolicy{BaseDelay: time.Millisecond, MaxDelay: time.Millisecond, NoJitter: true}
	if got := p.wait(1, 2*time.Second); got != 2*time.Second {
		t.Fatalf("wait = %v", got)
	}
	if got := p.wait(1, time.Hour); got != maxRetryAfter {
		t.Fatalf("wait = %v, want the %v cap", got, maxRetryAfter)
	}
}

// TestBackoffDoublesIsCappedAndJittersWithinBounds
// pins the backoff table (doubling, capped), that jitter stays between half the delay and the delay, and the default attempt count.
func TestBackoffDoublesIsCappedAndJittersWithinBounds(t *testing.T) {
	p := RetryPolicy{BaseDelay: 100 * time.Millisecond, MaxDelay: time.Second, NoJitter: true}
	want := []time.Duration{100, 200, 400, 800, 1000, 1000}
	for i, w := range want {
		if got := p.wait(i+1, 0); got != w*time.Millisecond {
			t.Fatalf("attempt %d: wait = %v, want %v", i+1, got, w*time.Millisecond)
		}
	}
	p.NoJitter = false
	for range 200 {
		if got := p.wait(3, 0); got < 200*time.Millisecond || got > 400*time.Millisecond {
			t.Fatalf("jittered wait %v outside [200ms, 400ms]", got)
		}
	}
	if (RetryPolicy{}).attempts() != 4 || (RetryPolicy{MaxAttempts: 7}).attempts() != 7 {
		t.Fatal("attempts default")
	}
}

// TestParseRetryAfter
// pins that Retry-After is read as seconds and that an empty, zero, negative or HTTP-date value is 0.
func TestParseRetryAfter(t *testing.T) {
	for in, want := range map[string]time.Duration{"3": 3 * time.Second, "": 0, "0": 0, "-1": 0, "Wed, 21 Oct 2026 07:28:00 GMT": 0} {
		if got := parseRetryAfter(http.Header{"Retry-After": {in}}); got != want {
			t.Errorf("Retry-After %q = %v, want %v", in, got, want)
		}
	}
}

// TestCorrelationIDAndHeadersAndQuery
// pins that the correlation id of the context, the User-Agent and the encoded query reach the server.
func TestCorrelationIDAndHeadersAndQuery(t *testing.T) {
	s := newStack(t)
	s.api = func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get(CorrelationHeader) != "req-42" || !strings.HasPrefix(r.Header.Get("User-Agent"), "smo-sdk-go/") || r.URL.RawQuery != "limit=5&state=RUNNING" {
			t.Errorf("headers %v query %q", r.Header, r.URL.RawQuery)
		}
		writeJSON(w, 200, nil)
	}
	q := map[string][]string{"state": {"RUNNING"}, "limit": {"5"}}
	err := s.client(t).Do(WithCorrelationID(context.Background(), "req-42"), Request{Method: "GET", Path: "/rapp-mgmt/instances", Query: q}, nil)
	if err != nil {
		t.Fatal(err)
	}
}

// TestNewValidatesTheConfig
// pins the rejected gateway URLs and half-pinned invoker, the defaults, and the removal of a trailing slash.
func TestNewValidatesTheConfig(t *testing.T) {
	for name, cfg := range map[string]Config{
		"scheme":      {GatewayURL: "ftp://x"},
		"no host":     {GatewayURL: "http://"},
		"query":       {GatewayURL: "http://x?a=1"},
		"half pinned": {GatewayURL: "http://x", InvokerID: "id"},
	} {
		if _, err := New(cfg); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
	c, err := New(Config{})
	if err != nil || c.gateway != "http://r1-termination:8000" || c.cfg.Name != "go-rapp" {
		t.Fatalf("defaults: %v %+v", err, c)
	}
	c, _ = New(Config{GatewayURL: "http://x:1/"})
	if c.gateway != "http://x:1" {
		t.Fatalf("gateway = %q", c.gateway)
	}
}

// TestDoRejectsAPathWithoutALeadingSlash
// pins that a path that does not start with / is an error, since it would be glued to the gateway host.
func TestDoRejectsAPathWithoutALeadingSlash(t *testing.T) {
	c, _ := New(Config{BearerToken: "t"})
	if err := c.Do(context.Background(), Request{Method: "GET", Path: "dme"}, nil); err == nil {
		t.Fatal("expected an error")
	}
}

// TestAnUndecodableAnswerIsAnError
// pins that a 2xx body that is not JSON is an error naming the decoding, not a silent empty result.
func TestAnUndecodableAnswerIsAnError(t *testing.T) {
	s := newStack(t)
	s.api = func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("<html>")) }
	var out Object
	if err := s.client(t).Do(context.Background(), Request{Method: "GET", Path: "/x"}, &out); err == nil || !strings.Contains(err.Error(), "decoding") {
		t.Fatalf("err = %v", err)
	}
}

// TestA204AndAnEmptyBodyDecodeToNothing
// pins that a 204 leaves the output untouched and is not an error.
func TestA204AndAnEmptyBodyDecodeToNothing(t *testing.T) {
	s := newStack(t)
	s.api = func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(204) }
	var out Object
	if err := s.client(t).Do(context.Background(), Request{Method: "DELETE", Path: "/x"}, &out); err != nil || out != nil {
		t.Fatalf("err=%v out=%v", err, out)
	}
}
