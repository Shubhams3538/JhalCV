package main

import (
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"time"
)

func main() {
	port := flag.Int("port", 0, "Local port (default: choose an available port)")
	noBrowser := flag.Bool("no-browser", false, "Print the URL without opening a browser")
	root := flag.String("root", "..", "Folder that holds web/, templates/ and data/ (the Python project)")
	flag.Parse()

	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", *port))
	if err != nil {
		fmt.Fprintf(os.Stderr, "Could not start the local app: %v\n", err)
		os.Exit(1)
	}
	actualPort := listener.Addr().(*net.TCPAddr).Port
	url := fmt.Sprintf("http://127.0.0.1:%d", actualPort)

	server := &http.Server{
		Handler:           requireLocalHost(actualPort, newRouter(filepath.Join(*root, "web"))),
		ReadHeaderTimeout: 10 * time.Second,
	}

	fmt.Printf("JhalCV is running at %s\nPress Ctrl+C to stop.\n", url)
	if !*noBrowser {
		openBrowser(url)
	}

	log.Fatal(server.Serve(listener))
}

// openBrowser asks the operating system to open url in the default browser.
func openBrowser(url string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", url)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	if err := cmd.Start(); err != nil {
		fmt.Fprintf(os.Stderr, "Could not open a browser (%v). Open %s yourself.\n", err, url)
	}
}