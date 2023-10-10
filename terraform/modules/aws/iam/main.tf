
locals {
  workload_role_name = "keda-workload-1"
  workload_trust_relations = jsonencode(
    [for role in aws_iam_role.roles :
      {
        Sid : "",
        Effect : "Allow"
        Action : "sts:AssumeRole",
        Principal : {
          "AWS" : role.arn
        }
    }]
  )
}

resource "aws_iam_user" "e2e_test" {
  name = "e2e-test-user"
  path = "/"

  tags = var.tags
}

resource "aws_iam_access_key" "e2e_test" {
  user = aws_iam_user.e2e_test.name
}

resource "aws_iam_user_policy_attachment" "user_assignement" {
  user       = aws_iam_user.e2e_test.name
  policy_arn = aws_iam_policy.policy.arn
}

data "tls_certificate" "certs" {
  count = length(var.identity_providers)
  url   = var.identity_providers[count.index].oidc_issuer_url
}

resource "aws_iam_openid_connect_provider" "oidc_providers" {
  count = length(data.tls_certificate.certs)
  url   = var.identity_providers[count.index].oidc_issuer_url

  client_id_list = [
    "sts.amazonaws.com",
  ]
  thumbprint_list = [data.tls_certificate.certs[count.index].certificates[0].sha1_fingerprint]
  tags            = var.tags
}


resource "aws_iam_role" "roles" {
  count = length(aws_iam_openid_connect_provider.oidc_providers)
  name  = var.identity_providers[count.index].role_name
  tags  = var.tags

  assume_role_policy = <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Federated": "${aws_iam_openid_connect_provider.oidc_providers[count.index].arn}"
            },
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {
                "StringEquals": {
                    "${replace(var.identity_providers[count.index].oidc_issuer_url, "https://", "")}:sub": "system:serviceaccount:keda:keda-operator",
                    "${replace(var.identity_providers[count.index].oidc_issuer_url, "https://", "")}:aud": "sts.amazonaws.com"
                }
            }
        }
    ]
}
EOF
}

resource "aws_iam_role_policy_attachment" "role_assignements" {
  count      = length(aws_iam_role.roles)
  role       = aws_iam_role.roles[count.index].name
  policy_arn = aws_iam_policy.policy.arn
}

resource "aws_iam_policy" "policy" {
  name = "e2e-test-policy"
  tags = var.tags

  policy = <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "dynamodb:*",
            "Resource": [
                "arn:aws:dynamodb:*:589761922677:table/*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": "dynamodb:*",
            "Resource": [
                "arn:aws:dynamodb:*:589761922677:table/*/stream/*",
                "arn:aws:dynamodb:*:589761922677:table/*/index/*",
                "arn:aws:dynamodb:*:589761922677:table/*/backup/*",
                "arn:aws:dynamodb:*:589761922677:table/*/export/*",
                "arn:aws:dynamodb::589761922677:global-table/*",
                "arn:aws:dynamodb:*:589761922677:table/*/import/*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": [
                "cloudwatch:PutMetricData",
                "cloudwatch:GetMetricData",
                "dynamodb:ListTables",
                "kinesis:ListShards",
                "dynamodb:PurchaseReservedCapacityOfferings",
                "cloudwatch:DeleteAnomalyDetector",
                "cloudwatch:ListMetrics",
                "cloudwatch:DescribeAnomalyDetectors",
                "kinesis:ListStreams",
                "dynamodb:DescribeReservedCapacityOfferings",
                "cloudwatch:DescribeAlarmsForMetric",
                "cloudwatch:ListDashboards",
                "cloudwatch:PutAnomalyDetector",
                "dynamodb:ListImports",
                "cloudwatch:GetMetricWidgetImage",
                "dynamodb:DescribeLimits",
                "dynamodb:ListExports",
                "kinesis:DescribeLimits",
                "kinesis:DisableEnhancedMonitoring",
                "cloudwatch:PutManagedInsightRules",
                "cloudwatch:DescribeInsightRules",
                "sqs:ListQueues",
                "kinesis:EnableEnhancedMonitoring",
                "dynamodb:ListBackups",
                "cloudwatch:GetMetricStatistics",
                "cloudwatch:ListManagedInsightRules",
                "dynamodb:ListStreams",
                "kinesis:UpdateStreamMode",
                "dynamodb:ListContributorInsights",
                "dynamodb:ListGlobalTables",
                "cloudwatch:ListMetricStreams",
                "dynamodb:DescribeReservedCapacity",
                "kinesis:UpdateShardCount"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": "sqs:*",
            "Resource": [
                "arn:aws:sqs:*:589761922677:*"
            ]
        },
        {
            "Effect": "Deny",
            "Action": "sqs:*",
            "Resource": [
                "arn:aws:sqs:*:589761922677:asume-role-queue-*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": "cloudwatch:*",
            "Resource": [
                "arn:aws:cloudwatch:*:589761922677:alarm:*",
                "arn:aws:cloudwatch:*:589761922677:metric-stream/*",
                "arn:aws:cloudwatch:*:589761922677:insight-rule/*",
                "arn:aws:cloudwatch::589761922677:dashboard/*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": "kinesis:*",
            "Resource": [
                "arn:aws:kms:*:589761922677:key/*",
                "arn:aws:kinesis:*:589761922677:*/*/consumer/*:*",
                "arn:aws:kinesis:*:589761922677:stream/*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": [
                "arn:aws:iam::*:role/${local.workload_role_name}"
            ]
        }
    ]
}
EOF
}

resource "aws_iam_role" "workload_role" {
  name = local.workload_role_name
  tags = var.tags

  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": ${local.workload_trust_relations}
}
EOF
}

resource "aws_iam_role_policy_attachment" "workload_role_assignements" {
  role       = aws_iam_role.workload_role.name
  policy_arn = aws_iam_policy.workload_role_policy.arn
}


resource "aws_iam_policy" "workload_role_policy" {
  name = "e2e-test-assume-role-policy"
  tags = var.tags

  policy = <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "sqs:*",
            "Resource": "arn:aws:sqs:*:589761922677:asume-role-queue-*"
        }
    ]
}
EOF
}