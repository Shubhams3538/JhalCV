package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"syscall"
	"time"
)

func main() {
	port := flag.Int("port", 0, "Local port (default: choose an available port)")
	noBrowser := flag.Bool("no-browser", false, "Print the URL without opening a browser")
	root := flag.String("root", "..", "Folder that holds web/, templates/ and data/ (the Python project)")
	flag.Parse()

	// ctx is "cancelled" when the user presses Ctrl+C (SIGINT) or the OS asks
	// the program to stop (SIGTERM). Set up first so no early Ctrl+C is missed.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

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

	// Serve in the background so main() stays free to wait for Ctrl+C.
	go func() {
		if err := server.Serve(listener); !errors.Is(err, http.ErrServerClosed) {
			log.Fatal(err)
		}
	}()

	fmt.Printf("JhalCV is running at %s\nPress Ctrl+C to stop.\n", url)
	if !*noBrowser {
		openBrowser(url)
	}

	<-ctx.Done() // Block here until Ctrl+C.

	fmt.Println("\nStopped.")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		fmt.Fprintf(os.Stderr, "Could not stop cleanly: %v\n", err)
	}
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