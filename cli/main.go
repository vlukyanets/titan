// Command titan is the TITAN CLI.
package main

import (
	"fmt"
	"io"
	"os"
)

const usage = "usage: titan <command>\n"

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run executes the CLI with args and returns the exit code.
func run(args []string, stdout, stderr io.Writer) int {
	fmt.Fprint(stderr, usage)
	return 2
}
