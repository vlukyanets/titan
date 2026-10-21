package main

import (
	"bytes"
	"testing"
)

func TestNoCommandPrintsUsage(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run(nil, &stdout, &stderr); code != 2 {
		t.Errorf("exit code = %d, want 2", code)
	}
	if stderr.String() != usage {
		t.Errorf("stderr = %q, want %q", stderr.String(), usage)
	}
}
