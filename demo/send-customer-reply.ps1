# Demo: a customer replies by email, to a lead (Lead Scoring) or to a BPO client (Churn Prediction).
# Hands a real-format email to Odoo's mail gateway (the same code path as an email fetched
# from the sales inbox), so it lands in the record's chatter as a customer email and the
# score updates.
#
# Usage (PowerShell, from the project folder, Odoo running):
#   powershell -ExecutionPolicy Bypass -File demo\send-customer-reply.ps1 -Lead "Bayside" -Body "We need about 40 agents starting next month."
#   powershell -ExecutionPolicy Bypass -File demo\send-customer-reply.ps1 -Client "Acme Corporation" -Body "We are reviewing other vendors." -DaysAgo 10

param(
    [string]$Lead,                                 # part of the lead name, or...
    [string]$Client,                               # ...part of the client (contact) name
    [Parameter(Mandatory = $true)][string]$Body,   # what the customer writes
    [int]$DaysAgo = 0,                             # date the email in the past (e.g. for the sentiment trend)
    [string]$Url = 'http://localhost:8069',
    [string]$Database = 'smarterp',
    [string]$User = 'admin',
    [string]$Password = 'admin'
)

if ([bool]$Lead -eq [bool]$Client) { throw 'Give exactly one of -Lead or -Client' }
if ($Lead) {
    $model = 'crm.lead'; $name = $Lead; $emailField = 'email_from'; $scoreFields = @('smarterp_score', 'smarterp_tier')
} else {
    $model = 'res.partner'; $name = $Client; $emailField = 'email'; $scoreFields = @('smarterp_churn_score', 'smarterp_health')
}

function Invoke-OdooRpc($Service, $Method, $Arguments) {
    $request = @{ jsonrpc = '2.0'; method = 'call'; params = @{ service = $Service; method = $Method; args = $Arguments } }
    $response = Invoke-RestMethod -Uri "$Url/jsonrpc" -Method Post -ContentType 'application/json' `
        -Body ([System.Text.Encoding]::UTF8.GetBytes(($request | ConvertTo-Json -Depth 10)))
    if ($response.error) { throw $response.error.data.message }
    return $response.result
}

$uid = Invoke-OdooRpc 'common' 'login' @($Database, $User, $Password)
if (-not $uid) { throw "Login failed for $User on $Database" }

$found = Invoke-OdooRpc 'object' 'execute_kw' @($Database, $uid, $Password, $model, 'search_read',
    @(, @(, @('name', 'ilike', $name))), @{ fields = @('id', 'name', $emailField) + $scoreFields; limit = 1 })
if (-not $found) { throw "No $model found matching '$name'" }
$record = $found[0]
$from = $record.$emailField
if (-not $from) { throw "'$($record.name)' has no email address to reply from" }
Write-Host "Before: $($record.name) -> $($record.($scoreFields[0])) $($record.($scoreFields[1]))"

# Address the reply to our own user, so Odoo doesn't suggest a made-up inbox as a recipient later.
$ourEmail = (Invoke-OdooRpc 'object' 'execute_kw' @($Database, $uid, $Password, 'res.users', 'read',
    @(, @($uid)), @{ fields = @('email') }))[0].email

$email = @"
From: $from
To: $ourEmail
Subject: Re: $($record.name)
Message-ID: <demo-reply-$([guid]::NewGuid())@smarterp-demo.example>
Date: $((Get-Date).ToUniversalTime().AddDays(-$DaysAgo).ToString('r'))
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<p>$([System.Net.WebUtility]::HtmlEncode($Body))</p>
"@

Invoke-OdooRpc 'object' 'execute_kw' @($Database, $uid, $Password, 'mail.thread', 'message_process',
    @($model, $email), @{ thread_id = $record.id }) | Out-Null

$after = (Invoke-OdooRpc 'object' 'execute_kw' @($Database, $uid, $Password, $model, 'read',
    @(, @($record.id)), @{ fields = $scoreFields + @('smarterp_confidence') }))[0]
Write-Host "After:  $($record.name) -> $($after.($scoreFields[0])) $($after.($scoreFields[1])) ($($after.smarterp_confidence)% confidence)"
