package main

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"text/tabwriter"
)

type model struct {
	Name    string `json:"name"`
	URL     string `json:"url"`
	HasKey  bool   `json:"has_key"`
	Current bool   `json:"current"`
}

// showSettings lists the user's models, the current one marked with a
// star; for a key, only whether one is set.
func showSettings(stdout io.Writer) error {
	var settings struct {
		Models []model `json:"models"`
	}
	if err := call(http.MethodGet, "/settings", nil, &settings); err != nil {
		return err
	}
	if len(settings.Models) == 0 {
		fmt.Fprintln(stdout, "No models; run titan settings model add")
		return nil
	}
	w := tabwriter.NewWriter(stdout, 0, 0, 2, ' ', 0)
	for _, m := range settings.Models {
		mark, key := " ", "no key"
		if m.Current {
			mark = "*"
		}
		if m.HasKey {
			key = "key set"
		}
		fmt.Fprintf(w, "%s %s\t%s\t%s\n", mark, m.Name, m.URL, key)
	}
	return w.Flush()
}

// addModel asks for a model's URL, name and key and saves it on the node;
// a model of the same name is replaced.
func addModel(stdin io.Reader, prompts io.Writer) error {
	in := bufio.NewScanner(stdin)
	modelURL := ask(in, prompts, "URL: ")
	name := ask(in, prompts, "Model: ")
	if modelURL == "" || name == "" {
		return errors.New("the URL and the model name are required")
	}
	key, err := askSecret(in, stdin, prompts, "Key (empty for none): ")
	if err != nil {
		return err
	}
	body := map[string]string{"url": modelURL, "key": key}
	if err := call(http.MethodPut, modelPath(name), body, nil); err != nil {
		return err
	}
	fmt.Fprintln(prompts, "Saved.")
	return nil
}

func useModel(name string) error {
	body := map[string]string{"name": name}
	return noSuchModel(name, call(http.MethodPut, "/settings/current-model", body, nil))
}

func removeModel(name string) error {
	return noSuchModel(name, call(http.MethodDelete, modelPath(name), nil, nil))
}

// modelPath escapes the name, which may hold a slash.
func modelPath(name string) string {
	return "/settings/models/" + url.PathEscape(name)
}

func noSuchModel(name string, err error) error {
	if errors.Is(err, errNotFound) {
		return fmt.Errorf("no model named %q; titan settings lists them", name)
	}
	return err
}
