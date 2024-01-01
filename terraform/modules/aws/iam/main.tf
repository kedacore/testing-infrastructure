
locals {
  keda_role_name      = "keda-operator"
  workload1_role_name = "keda-workload-1"
  workload2_role_name = "keda-workload-2"
  keda_clusters_trusted_relations = jsonencode(
    [for index, provider in aws_iam_openid_connect_provider.oidc_providers :
      {
        Effect : "Allow",
        Principal : {
          "Federated" : "${provider.arn}"
        },
        Action : "sts:AssumeRoleWithWebIdentity",
        Condition : {
          StringEquals : {
            "${replace(var.identity_providers[index].oidc_issuer_url, "https://", "")}:sub" : "system:serviceaccount:keda:keda-operator",
            "${replace(var.identity_providers[index].oidc_issuer_url, "https://", "")}:aud" : "sts.amazonaws.com"
          }
        }
      }
    ]
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

resource "aws_iam_role" "keda_role" {
  name = local.keda_role_name
  tags = var.tags

  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": ${local.keda_clusters_trusted_relations}
}
EOF
}

resource "aws_iam_role_policy_attachment" "keda_role_assignement" {
  role       = aws_iam_role.keda_role.name
  policy_arn = aws_iam_policy.policy.arn
}

// TO REMOVE AFTER MERGING https://github.com/kedacore/keda/pull/5061
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
// END TO REMOVE

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
                "secretsmanager:CreateSecret",
                "secretsmanager:GetSecretValue",
                "secretsmanager:DeleteSecret",
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
            "Action": "sqs:GetQueueAttributes",
            "Resource": [
                "arn:aws:sqs:*:589761922677:assume-role-*"
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
                "arn:aws:iam::*:role/${local.workload1_role_name}"
            ]
        }
    ]
}
EOF
}

resource "aws_iam_role" "workload1_role" {
  name = local.workload1_role_name
  tags = var.tags

  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": {
    "Sid" : "",
    "Effect" : "Allow"
    "Action" : "sts:AssumeRole",
    "Principal" : {
      "AWS" : "${aws_iam_role.keda_role.arn}"
    }
  }
}
EOF
}

resource "aws_iam_role" "workload2_role" {
  name = local.workload2_role_name
  tags = var.tags

  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": ${local.keda_clusters_trusted_relations}
}
EOF
}

resource "aws_iam_policy" "workload1_role_policy" {
  name = "e2e-test-assume-role-policy-workload1"
  tags = var.tags

  policy = <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "sqs:GetQueueAttributes",
            "Resource": "arn:aws:sqs:*:589761922677:assume-role-workload1-queue-*"
        }
    ]
}
EOF
}

resource "aws_iam_policy" "workload2_role_policy" {
  name = "e2e-test-assume-role-policy-workload2"
  tags = var.tags

  policy = <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "sqs:GetQueueAttributes",
            "Resource": "arn:aws:sqs:*:589761922677:assume-role-workload2-queue-*"
        }
    ]
}
EOF
}

resource "aws_iam_role_policy_attachment" "workload1_role_assignement" {
  role       = aws_iam_role.workload1_role.name
  policy_arn = aws_iam_policy.workload1_role_policy.arn
}

resource "aws_iam_role_policy_attachment" "workload2_role_assignement" {
  role       = aws_iam_role.workload2_role.name
  policy_arn = aws_iam_policy.workload2_role_policy.arn
}
