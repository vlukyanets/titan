package main

import (
	"bytes"
	"strings"
	"testing"
)

// signedIn signs alice in to a fake node and returns the run of a command.
func signedIn(t *testing.T) func(stdin string, args ...string) (int, string, string) {
	t.Helper()
	node, _ := fakeNode(t), configDir(t)
	var stdout, stderr bytes.Buffer
	if code := run([]string{"login", node.URL}, strings.NewReader("alice\nc0de\n"), &stdout, &stderr); code != 0 {
		t.Fatalf("login: exit code = %d, stderr = %q", code, stderr.String())
	}
	return func(stdin string, args ...string) (int, string, string) {
		var stdout, stderr bytes.Buffer
		code := run(args, strings.NewReader(stdin), &stdout, &stderr)
		return code, stdout.String(), stderr.String()
	}
}

func TestSettingsListModelsAndOnlyWhetherKeyIsSet(t *testing.T) {
	titan := signedIn(t)
	if code, _, stderr := titan("https://models.example/v1\nopenai/gpt-4o\nsk-secret\n", "settings", "model", "add"); code != 0 {
		t.Fatalf("model add: exit code = %d, stderr = %q", code, stderr)
	}
	titan("http://192.168.1.10:11434/v1\nllama\n\n", "settings", "model", "add")
	code, stdout, stderr := titan("", "settings")
	if code != 0 {
		t.Fatalf("settings: exit code = %d, stderr = %q", code, stderr)
	}
	want := "  llama          http://192.168.1.10:11434/v1  no key\n" +
		"* openai/gpt-4o  https://models.example/v1     key set\n"
	if stdout != want {
		t.Errorf("stdout =\n%s\nwant\n%s", stdout, want)
	}
	if strings.Contains(stdout+stderr, "sk-secret") {
		t.Error("settings printed the key")
	}
}

func TestUseAndRemoveModel(t *testing.T) {
	titan := signedIn(t)
	titan("https://models.example/v1\nbig\nkey\n", "settings", "model", "add")
	titan("http://localhost:8080/v1\nsmall\n\n", "settings", "model", "add")
	if code, _, stderr := titan("", "settings", "model", "use", "small"); code != 0 {
		t.Fatalf("model use: exit code = %d, stderr = %q", code, stderr)
	}
	if _, stdout, _ := titan("", "settings"); !strings.Contains(stdout, "* small") {
		t.Errorf("small is not current: %q", stdout)
	}
	if code, _, stderr := titan("", "settings", "model", "remove", "big"); code != 0 {
		t.Fatalf("model remove: exit code = %d, stderr = %q", code, stderr)
	}
	if _, stdout, _ := titan("", "settings"); strings.Contains(stdout, "big") {
		t.Errorf("big is still listed: %q", stdout)
	}
}

func TestUnknownModelIsNamed(t *testing.T) {
	titan := signedIn(t)
	for _, command := range []string{"use", "remove"} {
		code, _, stderr := titan("", "settings", "model", command, "nope")
		if code != 1 || !strings.Contains(stderr, `no model named "nope"`) {
			t.Errorf("%s: exit code = %d, stderr = %q", command, code, stderr)
		}
	}
}

func TestEmptyURLOrModelIsRefused(t *testing.T) {
	for _, stdin := range []string{"\nbig\nkey\n", "https://models.example/v1\n  \nkey\n"} {
		titan := signedIn(t)
		if code, _, stderr := titan(stdin, "settings", "model", "add"); code != 1 || !strings.Contains(stderr, "required") {
			t.Errorf("stdin %q: exit code = %d, stderr = %q", stdin, code, stderr)
		}
		if _, stdout, _ := titan("", "settings"); !strings.HasPrefix(stdout, "No models") {
			t.Errorf("stdin %q: a refused model was saved: %q", stdin, stdout)
		}
	}
}

func TestSettingsBeforeAModelIsAdded(t *testing.T) {
	titan := signedIn(t)
	_, stdout, _ := titan("", "settings")
	if stdout != "No models; run titan settings model add\n" {
		t.Errorf("stdout = %q", stdout)
	}
}
