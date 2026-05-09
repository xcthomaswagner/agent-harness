package dashboard

import (
	"crypto/subtle"
	"html/template"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"
)

const apiKeyCookie = "operator_api_key"

type Config struct {
	BackendURL   *url.URL
	DashboardKey string
}

type Server struct {
	cfg      Config
	mux      *http.ServeMux
	proxy    *httputil.ReverseProxy
	shellTpl *template.Template
}

func NewServer(cfg Config) *Server {
	if cfg.BackendURL == nil {
		cfg.BackendURL = &url.URL{Scheme: "http", Host: "127.0.0.1:8000"}
	}
	s := &Server{
		cfg:      cfg,
		mux:      http.NewServeMux(),
		shellTpl: template.Must(template.New("shell").Parse(shellHTML)),
	}
	s.proxy = newProxy(cfg.BackendURL, s.apiKeyForRequest)
	s.routes()
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

func (s *Server) routes() {
	s.mux.HandleFunc("/", s.handleRoot)
	s.mux.HandleFunc("/operator/operator-go.css", s.handleCSS)
	s.mux.HandleFunc("/operator/operator-go.js", s.handleJS)
	s.mux.HandleFunc("/operator", s.handleShell)
	s.mux.HandleFunc("/operator/", s.handleShell)
	s.mux.HandleFunc("/api/operator/", s.handleProxy)
	s.mux.HandleFunc("/api/learning/", s.handleProxy)
	s.mux.HandleFunc("/api/traces/", s.handleProxy)
}

func (s *Server) handleRoot(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	http.Redirect(w, r, "/operator/", http.StatusFound)
}

func (s *Server) handleCSS(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/css; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write([]byte(appCSS))
}

func (s *Server) handleJS(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write([]byte(appJS))
}

func (s *Server) handleShell(w http.ResponseWriter, r *http.Request) {
	if isAssetLookalike(r.URL.Path) {
		http.NotFound(w, r)
		return
	}
	if !s.authorized(r) {
		http.Error(w, "Invalid or missing dashboard API key", http.StatusUnauthorized)
		return
	}
	if key := keyFromRequest(r); key != "" {
		http.SetCookie(w, &http.Cookie{
			Name:     apiKeyCookie,
			Value:    key,
			Path:     "/",
			HttpOnly: true,
			SameSite: http.SameSiteLaxMode,
			Expires:  time.Now().Add(30 * 24 * time.Hour),
		})
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_ = s.shellTpl.Execute(w, map[string]string{
		"Backend": s.cfg.BackendURL.String(),
	})
}

func (s *Server) handleProxy(w http.ResponseWriter, r *http.Request) {
	if !s.authorized(r) {
		http.Error(w, "Invalid or missing dashboard API key", http.StatusUnauthorized)
		return
	}
	s.proxy.ServeHTTP(w, r)
}

func (s *Server) authorized(r *http.Request) bool {
	if s.cfg.DashboardKey == "" {
		return true
	}
	key := keyFromRequest(r)
	return key != "" && subtle.ConstantTimeCompare([]byte(key), []byte(s.cfg.DashboardKey)) == 1
}

func (s *Server) apiKeyForRequest(r *http.Request) string {
	if key := keyFromRequest(r); key != "" {
		return key
	}
	if s.cfg.DashboardKey != "" {
		return s.cfg.DashboardKey
	}
	return ""
}

func keyFromRequest(r *http.Request) string {
	if key := strings.TrimSpace(r.URL.Query().Get("api_key")); key != "" {
		return key
	}
	if key := strings.TrimSpace(r.Header.Get("X-API-Key")); key != "" {
		return key
	}
	if cookie, err := r.Cookie(apiKeyCookie); err == nil {
		return strings.TrimSpace(cookie.Value)
	}
	return ""
}

func newProxy(backend *url.URL, keyFn func(*http.Request) string) *httputil.ReverseProxy {
	proxy := httputil.NewSingleHostReverseProxy(backend)
	original := proxy.Director
	proxy.Director = func(r *http.Request) {
		original(r)
		r.URL.Scheme = backend.Scheme
		r.URL.Host = backend.Host
		r.Host = backend.Host
		if key := keyFn(r); key != "" {
			r.Header.Set("X-API-Key", key)
		}
	}
	return proxy
}

func isAssetLookalike(path string) bool {
	lower := strings.ToLower(path)
	return strings.HasSuffix(lower, ".js") ||
		strings.HasSuffix(lower, ".css") ||
		strings.HasSuffix(lower, ".json") ||
		strings.HasSuffix(lower, ".map") ||
		strings.HasSuffix(lower, ".ico") ||
		strings.HasSuffix(lower, ".png") ||
		strings.HasSuffix(lower, ".svg")
}
