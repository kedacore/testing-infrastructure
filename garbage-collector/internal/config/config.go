package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

const (
	DefaultMaxAgeHours             = 12
	DefaultAWSRegion               = "eu-west-2"
	PermanentServiceBusTopicSuffix = "-e2e-receive-event-grid-topic"
)

type Config struct {
	AzureSubscriptionID     string
	ExcludedSBTopicSuffixes []string
	AWSRegion               string
	GCPProjectID            string
	MaxAge                  time.Duration
	DryRun                  bool
}

func Load(dryRun bool) (Config, error) {
	maxAgeHours, err := readMaxAgeHours()
	if err != nil {
		return Config{}, err
	}

	// Provider credentials are validated by each cleaner, so a partial
	// environment is enough to run a subset of them.
	return Config{
		AzureSubscriptionID:     os.Getenv("AZURE_SUBSCRIPTION_ID"),
		ExcludedSBTopicSuffixes: []string{PermanentServiceBusTopicSuffix},
		AWSRegion:               DefaultAWSRegion,
		GCPProjectID:            os.Getenv("GCP_PROJECT_ID"),
		MaxAge:                  time.Duration(maxAgeHours) * time.Hour,
		DryRun:                  dryRun,
	}, nil
}

func readMaxAgeHours() (int, error) {
	value := getenvDefault("MAX_AGE_HOURS", fmt.Sprintf("%d", DefaultMaxAgeHours))
	hours, err := strconv.Atoi(value)
	if err != nil {
		return 0, fmt.Errorf("invalid MAX_AGE_HOURS %q: %w", value, err)
	}
	if hours < 1 {
		return 0, fmt.Errorf("MAX_AGE_HOURS must be >= 1, got %d", hours)
	}
	return hours, nil
}

func getenvDefault(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
