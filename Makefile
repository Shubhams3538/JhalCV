# Shortcuts for running JhalCV. Extra flags go in ARGS, for example:
#   make go ARGS="--port 8765 --no-browser"

.PHONY: go python test bench

# Build the Go backend and start it on this workspace (same frontend and data).
go:
	go build -C jhalCV_go -o bin/jhalcv ./cmd/jhalcv
	./jhalCV_go/bin/jhalcv --root . $(ARGS)

# Start the original Python backend.
python:
	python3 app.py $(ARGS)

# Check and test the Go port.
test:
	cd jhalCV_go && gofmt -l . && go vet ./... && go test ./...

# Compare Python and Go (see bench/README.md).
bench:
	python3 bench/run.py $(ARGS)
