package render

import (
	"fmt"
	"html/template"
	"strings"
	"time"
)

// toronto is the only timezone this site displays. Every close time in the
// database is a true instant; showing them in UTC is what makes an 18:00
// Toronto close read as 22:00 and look wrong to the person reading it.
var toronto = mustLoad("America/Toronto")

func mustLoad(name string) *time.Location {
	loc, err := time.LoadLocation(name)
	if err != nil {
		return time.UTC
	}
	return loc
}

func funcs() template.FuncMap {
	return template.FuncMap{
		"closeTime": func(t *time.Time) string {
			if t == nil {
				return "—"
			}
			return t.In(toronto).Format("Mon 15:04")
		},
		// dict builds a scope for an included fragment. html/template gives a
		// template exactly one argument, and the review bar needs four.
		"dict": func(kv ...any) (map[string]any, error) {
			if len(kv)%2 != 0 {
				return nil, fmt.Errorf("dict needs an even number of arguments, got %d", len(kv))
			}
			m := make(map[string]any, len(kv)/2)
			for i := 0; i < len(kv); i += 2 {
				key, ok := kv[i].(string)
				if !ok {
					return nil, fmt.Errorf("dict key %d is not a string", i)
				}
				m[key] = kv[i+1]
			}
			return m, nil
		},
		"hasPrefix": strings.HasPrefix,
		"money": func(v *float64) string {
			if v == nil {
				return "—"
			}
			return fmt.Sprintf("$%.2f", *v)
		},
		"untilClose": func(t *time.Time) string {
			if t == nil {
				return ""
			}
			d := time.Until(*t)
			if d <= 0 {
				return "closed"
			}
			if h := int(d.Hours()); h > 0 {
				return itoa(h) + "h"
			}
			return itoa(int(d.Minutes())) + "m"
		},
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}
