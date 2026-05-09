package dashboard

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestShellRequiresConfiguredDashboardKey(t *testing.T) {
	server := NewServer(Config{DashboardKey: "sekret", BackendURL: mustParseURL(t, "http://example.invalid")})

	res := httptest.NewRecorder()
	server.ServeHTTP(res, httptest.NewRequest(http.MethodGet, "/operator/", nil))
	if res.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", res.Code)
	}

	res = httptest.NewRecorder()
	server.ServeHTTP(res, httptest.NewRequest(http.MethodGet, "/operator/?api_key=sekret", nil))
	if res.Code != http.StatusOK {
		t.Fatalf("status with key = %d, want 200", res.Code)
	}
	if setCookie := res.Header().Get("Set-Cookie"); !strings.Contains(setCookie, apiKeyCookie+"=sekret") {
		t.Fatalf("Set-Cookie = %q, want operator API key cookie", setCookie)
	}
}

func TestProxyAddsDashboardKeyHeader(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("X-API-Key"); got != "sekret" {
			t.Fatalf("X-API-Key = %q, want sekret", got)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	}))
	defer backend.Close()

	server := NewServer(Config{DashboardKey: "sekret", BackendURL: mustParseURL(t, backend.URL)})
	req := httptest.NewRequest(http.MethodGet, "/api/operator/profiles", nil)
	req.Header.Set("X-API-Key", "sekret")
	res := httptest.NewRecorder()

	server.ServeHTTP(res, req)

	if res.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body=%s", res.Code, res.Body.String())
	}
	if !strings.Contains(res.Body.String(), `"ok":true`) {
		t.Fatalf("body = %q, want proxied JSON", res.Body.String())
	}
}

func TestAssetLookalikeUnderOperatorDoesNotServeShell(t *testing.T) {
	server := NewServer(Config{BackendURL: mustParseURL(t, "http://example.invalid")})
	res := httptest.NewRecorder()

	server.ServeHTTP(res, httptest.NewRequest(http.MethodGet, "/operator/not-real.js", nil))

	if res.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", res.Code)
	}
}

func mustParseURL(t *testing.T, raw string) *url.URL {
	t.Helper()
	parsed, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}
