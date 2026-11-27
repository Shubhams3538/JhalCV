// Package usererr marks errors whose message is written for the user, like
// ValueError in the Python app.
//
// The browser shows these messages word for word ("Filename: use letters...").
// Any other error is a bug or a system problem: the server logs its details
// and shows the user a generic message instead.
package usererr

import (
	"errors"
	"fmt"
)

// Error is an error with a message meant for the user.
type Error struct {
	msg string
}

func (e *Error) Error() string {
	return e.msg
}

// New returns a user-facing error with the message msg.
func New(msg string) error {
	return &Error{msg: msg}
}

// Errorf formats a user-facing error message, like fmt.Sprintf.
func Errorf(format string, args ...any) error {
	return &Error{msg: fmt.Sprintf(format, args...)}
}

// Message returns the user-facing message inside err, if it has one.
func Message(err error) (string, bool) {
	var e *Error
	if errors.As(err, &e) {
		return e.msg, true
	}
	return "", false
}
