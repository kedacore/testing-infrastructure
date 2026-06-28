package main

import (
	"context"
	"flag"
	"fmt"
	"os"

	"github.com/Azure/azure-sdk-for-go/sdk/azidentity"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"

	awsdynamodb "github.com/kedacore/testing-infrastructure/garbage-colletor/internal/aws/dynamodb"
	awskinesis "github.com/kedacore/testing-infrastructure/garbage-colletor/internal/aws/kinesis"
	awssqs "github.com/kedacore/testing-infrastructure/garbage-colletor/internal/aws/sqs"
	azureeventhub "github.com/kedacore/testing-infrastructure/garbage-colletor/internal/azure/eventhub"
	azureservicebus "github.com/kedacore/testing-infrastructure/garbage-colletor/internal/azure/servicebus"
	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/config"
	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/core"
)

func main() {
	ctx := context.Background()
	dryRun := flag.Bool("dry-run", true, "List candidates without deleting them")
	flag.Parse()

	cfg, err := config.Load(*dryRun)
	if err != nil {
		fmt.Printf("config error: %v\n", err)
		os.Exit(1)
	}

	cred, err := azidentity.NewDefaultAzureCredential(nil)
	if err != nil {
		fmt.Printf("azure credential error: %v\n", err)
		os.Exit(1)
	}

	awsCfg, err := awsconfig.LoadDefaultConfig(ctx, awsconfig.WithRegion(cfg.AWSRegion))
	if err != nil {
		fmt.Printf("aws config error: %v\n", err)
		os.Exit(1)
	}

	ehCleaner, err := azureeventhub.New(cred, cfg)
	if err != nil {
		fmt.Printf("azure eventhub cleaner error: %v\n", err)
		os.Exit(1)
	}
	sbCleaner, err := azureservicebus.New(cred, cfg)
	if err != nil {
		fmt.Printf("azure servicebus cleaner error: %v\n", err)
		os.Exit(1)
	}

	cleaners := []core.Cleaner{
		ehCleaner,
		sbCleaner,
		awssqs.New(awsCfg, cfg.DryRun, cfg.MaxAge),
		awskinesis.New(awsCfg, cfg.DryRun, cfg.MaxAge),
		awsdynamodb.New(awsCfg, cfg.DryRun, cfg.MaxAge),
	}

	exitCode := core.RunAll(ctx, cleaners)
	os.Exit(exitCode)
}
