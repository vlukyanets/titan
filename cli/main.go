// Command titan is the TITAN CLI.
package main

import (
	"fmt"
	"io"
	"os"
)

const usage = `usage: titan <command>

commands:
  login <node-url>  sign in with the code from the node's admin
  whoami            print the name you are signed in as
`

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

// run executes the CLI with args and returns the exit code.
func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	var err error
	switch {
	case len(args) == 2 && args[0] == "login":
		err = login(args[1], stdin, stderr)
	case len(args) == 1 && args[0] == "whoami":
		err = whoami(stdout)
	default:
		fmt.Fprint(stderr, usage)
		return 2
	}
	if err != nil {
		fmt.Fprintf(stderr, "titan: %v\n", err)
		return 1
	}
	return 0
}
