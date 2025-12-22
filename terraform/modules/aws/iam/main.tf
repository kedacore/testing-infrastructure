
locals {
  keda_role_name                = "keda-operator"
  workload1_role_name           = "keda-workload-1"
  workload2_role_name           = "keda-workload-2"
  workload_external_id_name     = "keda-workload-external-id"
  workload_external_id_value    = "keda-e2e-external-id-test"
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

// This is the primary role to be used for almost all the
// e2e tests. It allows any action over any (suported resource)
// except over the assume role queues. This role also allows
// to assume workload-1 role using sts:AssumeRole
resource "aws_iam_policy" "policy" {
  name = "e2e-test-policy"
  tags = var.tags

  policy = <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "aps:*",
                "cloudwatch:*",
                "dynamodb:*",
                "kinesis:*",
                "sqs:*",
                "secretsmanager:*",
                "kms:*"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Deny",
            "Action": "sqs:GetQueueAttributes",
            "Resource": [
                "arn:aws:sqs:*:589761922677:assume-role-*",
                "arn:aws:sqs:*:589761922677:external-id-queue-*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": [
                "arn:aws:iam::*:role/${local.workload1_role_name}",
                "arn:aws:iam::*:role/${local.workload_external_id_name}"
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
  "Statement": [
    {
      "Sid" : "",
      "Effect" : "Allow",
      "Action" : "sts:AssumeRole",
      "Principal" : {
        "AWS" : "${aws_iam_role.keda_role.arn}"
      }
    }
  ]
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

// This role requires an ExternalId to be assumed.
// Used for testing the externalID support in KEDA's pod identity.
resource "aws_iam_role" "workload_external_id_role" {
  name = local.workload_external_id_name
  tags = var.tags

  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Principal": {
        "AWS": "${aws_iam_role.keda_role.arn}"
      },
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "${local.workload_external_id_value}"
        }
      }
    }
  ]
}
EOF
}

resource "aws_iam_policy" "workload_external_id_policy" {
  name = "e2e-test-external-id-policy"
  tags = var.tags

  policy = <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "sqs:*",
            "Resource": "arn:aws:sqs:*:589761922677:external-id-queue-*"
        }
    ]
}
EOF
}

resource "aws_iam_role_policy_attachment" "workload_external_id_role_assignement" {
  role       = aws_iam_role.workload_external_id_role.name
  policy_arn = aws_iam_policy.workload_external_id_policy.arn
}
