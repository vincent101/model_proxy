package sankuai

import (
	"slices"
	"sync"
	"time"

	"github.com/google/uuid"
)

// Sessions are consumed on continuation so concurrent forks get separate IDs.
var ideSessions struct {
	sync.Mutex
	entries []sessionEntry
}

type sessionEntry struct {
	scope, id string
	prefix    []string
	at        time.Time
}

func sessionTake(scope string, prefix []string) string {
	ideSessions.Lock()
	defer ideSessions.Unlock()
	ideSessions.entries = slices.DeleteFunc(ideSessions.entries, func(s sessionEntry) bool { return time.Since(s.at) > 4*time.Hour })
	for i, s := range ideSessions.entries {
		if scope == s.scope && len(prefix) > len(s.prefix) && slices.Equal(prefix[:len(s.prefix)], s.prefix) {
			ideSessions.entries = slices.Delete(ideSessions.entries, i, i+1)
			return s.id
		}
	}
	return uuid.NewString()
}
func sessionPut(scope, id string, prefix []string) {
	ideSessions.Lock()
	defer ideSessions.Unlock()
	if len(ideSessions.entries) >= 32 {
		ideSessions.entries = ideSessions.entries[1:]
	}
	ideSessions.entries = append(ideSessions.entries, sessionEntry{scope, id, slices.Clone(prefix), time.Now()})
}
