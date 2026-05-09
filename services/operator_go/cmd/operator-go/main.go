package main

import (
	"log"
	"net/http"
	"net/url"
	"os"
	"strings"

	"github.com/xcthomaswagner/agent-harness/services/operator_go/internal/dashboard"
)

func main() {
	backendRaw := envDefault("OPERATOR_BACKEND_URL", "http://127.0.0.1:8000")
	backend, err := url.Parse(backendRaw)
	if err != nil {
		log.Fatalf("invalid OPERATOR_BACKEND_URL %q: %v", backendRaw, err)
	}
	if backend.Scheme == "" || backend.Host == "" {
		log.Fatalf("OPERATOR_BACKEND_URL must include scheme and host: %q", backendRaw)
	}

	addr := envDefault("OPERATOR_GO_ADDR", ":8081")
	server := dashboard.NewServer(dashboard.Config{
		BackendURL:   backend,
		DashboardKey: strings.TrimSpace(os.Getenv("DASHBOARD_API_KEY")),
	})

	log.Printf("operator-go listening on %s, proxying API calls to %s", addr, backend)
	log.Fatal(http.ListenAndServe(addr, server))
}

func envDefault(name string, fallback string) string {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	return value
}
