package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"strings"

	"github.com/Azure/azure-sdk-for-go/sdk/azcore"
	"github.com/Azure/azure-sdk-for-go/sdk/azidentity"
	awssdk "github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"

	awsamp "github.com/kedacore/testing-infrastructure/garbage-colletor/internal/aws/amp"
	awsdynamodb "github.com/kedacore/testing-infrastructure/garbage-colletor/internal/aws/dynamodb"
	awskinesis "github.com/kedacore/testing-infrastructure/garbage-colletor/internal/aws/kinesis"
	awssqs "github.com/kedacore/testing-infrastructure/garbage-colletor/internal/aws/sqs"
	azurecosmosdb "github.com/kedacore/testing-infrastructure/garbage-colletor/internal/azure/cosmosdb"
	azureeventhub "github.com/kedacore/testing-infrastructure/garbage-colletor/internal/azure/eventhub"
	azureservicebus "github.com/kedacore/testing-infrastructure/garbage-colletor/internal/azure/servicebus"
	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/config"
	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/core"
	gcpspanner "github.com/kedacore/testing-infrastructure/garbage-colletor/internal/gcp/spanner"
)

type cleanerFactory struct {
	name  string
	build func(ctx context.Context, cfg config.Config, p *providers) (core.Cleaner, error)
}

var factories = []cleanerFactory{
	{
		name: "azure-eventhub",
		build: func(_ context.Context, cfg config.Config, p *providers) (core.Cleaner, error) {
			cred, err := p.azureCredential(cfg)
			if err != nil {
				return nil, err
			}
			return azureeventhub.New(cred, cfg)
		},
	},
	{
		name: "azure-servicebus",
		build: func(_ context.Context, cfg config.Config, p *providers) (core.Cleaner, error) {
			cred, err := p.azureCredential(cfg)
			if err != nil {
				return nil, err
			}
			return azureservicebus.New(cred, cfg)
		},
	},
	{
		name: "azure-cosmosdb",
		build: func(_ context.Context, cfg config.Config, p *providers) (core.Cleaner, error) {
			cred, err := p.azureCredential(cfg)
			if err != nil {
				return nil, err
			}
			return azurecosmosdb.New(cred, cfg)
		},
	},
	{
		name: "aws-amp",
		build: func(ctx context.Context, cfg config.Config, p *providers) (core.Cleaner, error) {
			awsCfg, err := p.awsConfig(ctx, cfg)
			if err != nil {
				return nil, err
			}
			return awsamp.New(awsCfg, cfg.DryRun, cfg.MaxAge), nil
		},
	},
	{
		name: "aws-sqs",
		build: func(ctx context.Context, cfg config.Config, p *providers) (core.Cleaner, error) {
			awsCfg, err := p.awsConfig(ctx, cfg)
			if err != nil {
				return nil, err
			}
			return awssqs.New(awsCfg, cfg.DryRun, cfg.MaxAge), nil
		},
	},
	{
		name: "aws-kinesis",
		build: func(ctx context.Context, cfg config.Config, p *providers) (core.Cleaner, error) {
			awsCfg, err := p.awsConfig(ctx, cfg)
			if err != nil {
				return nil, err
			}
			return awskinesis.New(awsCfg, cfg.DryRun, cfg.MaxAge), nil
		},
	},
	{
		name: "aws-dynamodb",
		build: func(ctx context.Context, cfg config.Config, p *providers) (core.Cleaner, error) {
			awsCfg, err := p.awsConfig(ctx, cfg)
			if err != nil {
				return nil, err
			}
			return awsdynamodb.New(awsCfg, cfg.DryRun, cfg.MaxAge), nil
		},
	},
	{
		name: "gcp-spanner",
		build: func(ctx context.Context, cfg config.Config, _ *providers) (core.Cleaner, error) {
			if cfg.GCPProjectID == "" {
				return nil, fmt.Errorf("GCP_PROJECT_ID is required")
			}
			return gcpspanner.New(ctx, cfg)
		},
	},
}

// providers builds the cloud credentials lazily so a run only requires the ones
// needed by the selected cleaners.
type providers struct {
	azureCred azcore.TokenCredential
	awsCfg    *awssdk.Config
}

func (p *providers) azureCredential(cfg config.Config) (azcore.TokenCredential, error) {
	if cfg.AzureSubscriptionID == "" {
		return nil, fmt.Errorf("AZURE_SUBSCRIPTION_ID is required")
	}
	if p.azureCred != nil {
		return p.azureCred, nil
	}
	cred, err := azidentity.NewDefaultAzureCredential(nil)
	if err != nil {
		return nil, fmt.Errorf("azure credential error: %w", err)
	}
	p.azureCred = cred
	return cred, nil
}

func (p *providers) awsConfig(ctx context.Context, cfg config.Config) (awssdk.Config, error) {
	if p.awsCfg != nil {
		return *p.awsCfg, nil
	}
	awsCfg, err := awsconfig.LoadDefaultConfig(ctx, awsconfig.WithRegion(cfg.AWSRegion))
	if err != nil {
		return awssdk.Config{}, fmt.Errorf("aws config error: %w", err)
	}
	p.awsCfg = &awsCfg
	return awsCfg, nil
}

func main() {
	ctx := context.Background()
	dryRun := flag.Bool("dry-run", true, "List candidates without deleting them")
	only := flag.String("only", "", fmt.Sprintf("Comma separated list of cleaners to run (%s). Empty runs all", strings.Join(factoryNames(), ", ")))
	flag.Parse()

	cfg, err := config.Load(*dryRun)
	if err != nil {
		fmt.Printf("config error: %v\n", err)
		os.Exit(1)
	}

	selected, err := selectFactories(*only)
	if err != nil {
		fmt.Printf("cleaner selection error: %v\n", err)
		os.Exit(1)
	}

	p := &providers{}
	cleaners := make([]core.Cleaner, 0, len(selected))
	for _, factory := range selected {
		cleaner, err := factory.build(ctx, cfg, p)
		if err != nil {
			fmt.Printf("%s cleaner error: %v\n", factory.name, err)
			closeAll(cleaners)
			os.Exit(1)
		}
		cleaners = append(cleaners, cleaner)
	}

	exitCode := core.RunAll(ctx, cleaners)
	closeAll(cleaners)
	os.Exit(exitCode)
}

func selectFactories(only string) ([]cleanerFactory, error) {
	if strings.TrimSpace(only) == "" {
		return factories, nil
	}

	available := make(map[string]cleanerFactory, len(factories))
	for _, factory := range factories {
		available[factory.name] = factory
	}

	selected := make([]cleanerFactory, 0, len(factories))
	for _, name := range strings.Split(only, ",") {
		name = strings.TrimSpace(name)
		if name == "" {
			continue
		}
		factory, ok := available[name]
		if !ok {
			return nil, fmt.Errorf("unknown cleaner %q, available: %s", name, strings.Join(factoryNames(), ", "))
		}
		selected = append(selected, factory)
	}

	if len(selected) == 0 {
		return nil, fmt.Errorf("no cleaner selected, available: %s", strings.Join(factoryNames(), ", "))
	}
	return selected, nil
}

func factoryNames() []string {
	names := make([]string, 0, len(factories))
	for _, factory := range factories {
		names = append(names, factory.name)
	}
	return names
}

func closeAll(cleaners []core.Cleaner) {
	for _, cleaner := range cleaners {
		if closer, ok := cleaner.(interface{ Close() }); ok {
			closer.Close()
		}
	}
}
