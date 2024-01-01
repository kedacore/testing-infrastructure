output "e2e_user_access_key" {
  value = aws_iam_access_key.e2e_test.id
}

output "e2e_user_secret_key" {
  value = aws_iam_access_key.e2e_test.secret
}

output "keda_role_arn" {
  value = aws_iam_role.keda_role.arn
}

output "workload1_role_arn" {
  value = aws_iam_role.workload1_role.arn
}

output "workload2_role_arn" {
  value = aws_iam_role.workloa2_role.arn
}
