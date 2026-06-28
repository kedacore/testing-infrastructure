package core

import "context"

type Result struct {
	Name    string
	Found   int
	Deleted int
	Errors  int
	DryRun  bool
}

type Cleaner interface {
	Name() string
	Run(ctx context.Context) Result
}
