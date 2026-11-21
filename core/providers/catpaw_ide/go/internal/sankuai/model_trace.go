package sankuai

import (
	"context"
	"net/http"
	"strconv"
	"time"

	"catboss/internal/agenttrace"
)

var submitModelTrace = agenttrace.Submit

// modelRequest times the provider's HTTP request up to the response headers.
// This is one actual upstream attempt, not a completed generation or an Agent turn.
func (c *Client) modelRequest(ctx context.Context, id, model, session, target string, h http.Header, body []byte) (*http.Response, error) {
	if c.TraceOn == nil || !agenttrace.Provider(id) || !c.TraceOn(id) || c.owned == nil {
		return c.do(ctx, "POST", target, h, body)
	}
	start := time.Now()
	r, err := c.do(ctx, "POST", target, h, body)
	status := 0
	code := 2
	if r != nil {
		status = r.StatusCode
		if status/100 == 2 {
			code = 1
		}
	}
	e := agenttrace.Event{Source: id, Kind: "CLIENT", User: c.owned.MIS, Model: model, Trace: agenttrace.NewID(16), Span: agenttrace.NewID(8), Start: start, End: time.Now(), Status: code, Session: agenttrace.ID(id+"/"+session, 16), Provider: id}
	if code == 2 {
		e.StatusMessage = "No response headers"
		if status != 0 {
			e.StatusMessage = "HTTP " + strconv.Itoa(status)
		}
	}
	submitModelTrace(context.Background(), []agenttrace.Event{e}, func() bool { return c.TraceOn(id) })
	return r, err
}
