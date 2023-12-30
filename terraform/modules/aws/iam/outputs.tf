output "e2e_user_access_key" {
  value = aws_iam_access_key.e2e_test.id
}

output "e2e_user_secret_key" {
  value = aws_iam_access_key.e2e_test.secret
}

output "workload1_role_arn" {
  value = aws_iam_role.workload1_role.arn
}
