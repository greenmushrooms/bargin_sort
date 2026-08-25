// Package config reads the site's settings from the environment.
//
// Deliberately the scraper's own DB_* and FLARESOLVERR_* names, so one .env
// drives both programs and there is never a question of which credentials a
// refresh is using. Anything specific to the site is prefixed WEB_.
package config

import (
	"fmt"
	"net"
	"net/url"
	"os"
	"strconv"
)

type Config struct {
	DBHost     string
	DBPort     int
	DBName     string
	DBUser     string
	DBPassword string

	// Host defaults to all interfaces: this is read from a phone on the LAN as
	// often as from the machine it runs on.
	Host string
	Port int

	// There are ~90k open lots and a bare "usb" matches thousands of them. A
	// cap keeps a careless query from rendering a page nobody can read.
	SearchLimit int

	// A refresh is a real browser fetch through FlareSolverr and takes seconds.
	// The ceiling is short enough that a wedged FlareSolverr shows an error
	// rather than an eternal spinner.
	RefreshTimeoutSeconds int

	TemplateDir string
	StaticDir   string
	SQLDir      string

	// The refresh subprocess: the site venv's interpreter, and the directory
	// it runs in (web/, so scraper_bridge can find hibid_scraper beside it).
	PythonBin  string
	RefreshDir string
}

func Load() (Config, error) {
	c := Config{
		DBHost:                env("DB_HOST", "localhost"),
		DBName:                env("DB_NAME", "bargin_sort"),
		DBUser:                env("DB_USER", "user__bargin_sort"),
		DBPassword:            os.Getenv("DB_PASSWORD"),
		Host:                  env("WEB_HOST", "0.0.0.0"),
		TemplateDir:           env("WEB_TEMPLATE_DIR", "web/templates"),
		StaticDir:             env("WEB_STATIC_DIR", "web/static"),
		SQLDir:                env("WEB_SQL_DIR", "../sql"),
		PythonBin:             env("WEB_PYTHON", "../.venv/bin/python"),
		RefreshDir:            env("WEB_REFRESH_DIR", ".."),
		DBPort:                envInt("DB_PORT", 5432),
		Port:                  envInt("WEB_PORT", 7780),
		SearchLimit:           envInt("WEB_SEARCH_LIMIT", 200),
		RefreshTimeoutSeconds: envInt("WEB_REFRESH_TIMEOUT_S", 90),
	}
	if c.DBPassword == "" {
		return c, fmt.Errorf("DB_PASSWORD is not set (it is the app role's, held in the Prefect block bargin-sort--database-password)")
	}
	return c, nil
}

// DSN is built per call rather than cached: it holds the database password,
// and a long-lived package global is the sort of thing that ends up in a
// traceback.
func (c Config) DSN() string {
	// Built through net/url rather than fmt: the app role's password contains
	// URL-reserved characters, and interpolating it straight into a connection
	// string makes the parser read part of it as a port number.
	u := &url.URL{
		Scheme: "postgres",
		User:   url.UserPassword(c.DBUser, c.DBPassword),
		Host:   net.JoinHostPort(c.DBHost, strconv.Itoa(c.DBPort)),
		Path:   "/" + c.DBName,
	}
	return u.String()
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}
