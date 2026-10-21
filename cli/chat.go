package main

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// chatLoop sends each line the user types and prints the replies, until
// the input ends.
func chatLoop(stdin io.Reader, stdout, prompts io.Writer) error {
	// A Reader, not a Scanner: a pasted line may be longer than 64 KB.
	in := bufio.NewReader(stdin)
	// From a pipe or a file, the prompts would only clutter the output.
	if !isTerminal(stdin) {
		prompts = io.Discard
	}
	for {
		fmt.Fprint(prompts, "> ")
		line, readErr := in.ReadString('\n')
		if message := strings.TrimSpace(line); message != "" {
			if err := chat(message, stdout); err != nil {
				return err
			}
		}
		if readErr == io.EOF {
			fmt.Fprintln(prompts)
			return nil
		}
		if readErr != nil {
			return readErr
		}
	}
}

// chat sends one message and prints the reply as it comes.
func chat(message string, stdout io.Writer) error {
	if strings.TrimSpace(message) == "" {
		return errors.New("the message is empty")
	}
	resp, err := send(http.MethodPost, "/chat", map[string]string{"message": message})
	if errors.Is(err, errConflict) {
		return errors.New("no model set; run titan settings model add")
	}
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, err = io.Copy(stdout, resp.Body)
	fmt.Fprintln(stdout)
	if err != nil {
		// The node cuts the connection when the model's stream breaks.
		return errors.New("the reply was cut off")
	}
	return nil
}
