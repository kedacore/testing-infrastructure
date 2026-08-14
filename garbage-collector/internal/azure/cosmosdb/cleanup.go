package cosmosdb

import (
	"context"
	"fmt"
	"time"

	"github.com/Azure/azure-sdk-for-go/sdk/azcore"
	"github.com/Azure/azure-sdk-for-go/sdk/resourcemanager/cosmos/armcosmos/v3"
	"github.com/Azure/azure-sdk-for-go/sdk/resourcemanager/resources/armresources"

	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/config"
	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/core"
)

type Cleaner struct {
	rgClient       *armresources.ResourceGroupsClient
	accountsClient *armcosmos.DatabaseAccountsClient
	sqlClient      *armcosmos.SQLResourcesClient
	cfg            config.Config
}

func New(cred azcore.TokenCredential, cfg config.Config) (*Cleaner, error) {
	rgClient, err := armresources.NewResourceGroupsClient(cfg.AzureSubscriptionID, cred, nil)
	if err != nil {
		return nil, fmt.Errorf("creating azure resource groups client: %w", err)
	}
	accountsClient, err := armcosmos.NewDatabaseAccountsClient(cfg.AzureSubscriptionID, cred, nil)
	if err != nil {
		return nil, fmt.Errorf("creating azure cosmosdb accounts client: %w", err)
	}
	sqlClient, err := armcosmos.NewSQLResourcesClient(cfg.AzureSubscriptionID, cred, nil)
	if err != nil {
		return nil, fmt.Errorf("creating azure cosmosdb sql resources client: %w", err)
	}

	return &Cleaner{
		rgClient:       rgClient,
		accountsClient: accountsClient,
		sqlClient:      sqlClient,
		cfg:            cfg,
	}, nil
}

func (c *Cleaner) Name() string {
	return "azure-cosmosdb"
}

func (c *Cleaner) Run(ctx context.Context) core.Result {
	result := core.Result{Name: c.Name(), DryRun: c.cfg.DryRun}
	cutoff := time.Now().Add(-c.cfg.MaxAge)

	rgPager := c.rgClient.NewListPager(nil)
	for rgPager.More() {
		rgPage, err := rgPager.NextPage(ctx)
		if err != nil {
			fmt.Printf("[%s] listing resource groups failed: %v\n", c.Name(), err)
			result.Errors++
			break
		}

		for _, rg := range rgPage.Value {
			rgName := str(rg.Name)
			if rgName == "" {
				continue
			}

			accountPager := c.accountsClient.NewListByResourceGroupPager(rgName, nil)
			for accountPager.More() {
				accountPage, err := accountPager.NextPage(ctx)
				if err != nil {
					fmt.Printf("[%s] listing cosmosdb accounts failed in resource group %s: %v\n", c.Name(), rgName, err)
					result.Errors++
					break
				}

				for _, account := range accountPage.Value {
					accountName := str(account.Name)
					if accountName == "" {
						continue
					}

					c.cleanupSQLDatabases(ctx, rgName, accountName, cutoff, &result)
				}
			}
		}
	}

	return result
}

func (c *Cleaner) cleanupSQLDatabases(ctx context.Context, resourceGroup, account string, cutoff time.Time, result *core.Result) {
	pager := c.sqlClient.NewListSQLDatabasesPager(resourceGroup, account, nil)
	for pager.More() {
		page, err := pager.NextPage(ctx)
		if err != nil {
			fmt.Printf("[%s] listing sql databases failed in %s/%s: %v\n", c.Name(), resourceGroup, account, err)
			result.Errors++
			break
		}

		for _, db := range page.Value {
			result.Found++
			name := str(db.Name)
			if name == "" {
				continue
			}

			createdAt := databaseCreatedAt(db)
			if createdAt == nil || createdAt.After(cutoff) {
				continue
			}

			if c.cfg.DryRun {
				fmt.Printf("[%s][dry-run] would delete sql database %s/%s/%s (created %s)\n", c.Name(), resourceGroup, account, name, createdAt.UTC().Format(time.RFC3339))
				result.Deleted++
				continue
			}

			poller, err := c.sqlClient.BeginDeleteSQLDatabase(ctx, resourceGroup, account, name, nil)
			if err != nil {
				fmt.Printf("[%s] delete failed for sql database %s/%s/%s: %v\n", c.Name(), resourceGroup, account, name, err)
				result.Errors++
				continue
			}
			if _, err := poller.PollUntilDone(ctx, nil); err != nil {
				fmt.Printf("[%s] delete failed for sql database %s/%s/%s: %v\n", c.Name(), resourceGroup, account, name, err)
				result.Errors++
				continue
			}

			fmt.Printf("[%s] deleted sql database %s/%s/%s\n", c.Name(), resourceGroup, account, name)
			result.Deleted++
		}
	}
}

// Cosmos DB exposes the last update time as `_ts`, an epoch seconds timestamp.
func databaseCreatedAt(db *armcosmos.SQLDatabaseGetResults) *time.Time {
	if db == nil || db.Properties == nil || db.Properties.Resource == nil || db.Properties.Resource.Ts == nil {
		return nil
	}
	createdAt := time.Unix(int64(*db.Properties.Resource.Ts), 0)
	return &createdAt
}

func str(v *string) string {
	if v == nil {
		return ""
	}
	return *v
}
