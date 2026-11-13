package main

import (
	"flag"
	"fmt"
)

func main() {
	port := flag.Int("port", 0, "Local port (default: choose an available port)")
	noBrowser := flag.Bool("no-browser", false, "Print the URL without opening a browser")
	flag.Parse()

	fmt.Printf("port=%d noBrowser=%t\n", *port, *noBrowser)
}