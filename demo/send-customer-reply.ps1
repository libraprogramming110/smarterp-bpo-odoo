# Demo: a customer replies to a lead by email.
# Hands a real-format email to Odoo's mail gateway (the same code path as an email fetched
# from the sales inbox), so it lands in the lead's chatter as a customer email and the
# lead score updates.
#
# Usage (PowerShell, from the project folder, Odoo running):
#   powershell -ExecutionPolicy Bypass -File demo\send-customer-reply.ps1 -Lead "Bayside" -Body "We need about 40 agents starting next month."

param(
    [Parameter(Mandatory = $true)][string]$Lead,   # part of the lead name
    [Parameter(Mandatory = $true)][string]$Body,   # what the customer writes
    [string]$Url = 'http://localhost:8069',
    [string]$Database = 'smarterp',
    [string]$User = 'admin',
    [string]$Password = 'admin'
)

function Invoke-OdooRpc($Service, $Method, $Arguments) {
    $request = @{ jsonrpc = '2.0'; method = 'call'; params = @{ service = $Service; method = $Method; args = $Arguments } }
    $response = Invoke-RestMethod -Uri "$Url/jsonrpc" -Method Post -ContentType 'application/json' `
        -Body ([System.Text.Encoding]::UTF8.GetBytes(($request | ConvertTo-Json -Depth 10)))
    if ($response.error) { throw $response.error.data.message }
    return $response.result
}

$uid = Invoke-OdooRpc 'common' 'login' @($Database, $User, $Password)
if (-not $uid) { throw "Login failed for $User on $Database" }

$found = Invoke-OdooRpc 'object' 'execute_kw' @($Database, $uid, $Password, 'crm.lead', 'search_read',
    @(, @(, @('name', 'ilike', $Lead))), @{ fields = @('id', 'name', 'email_from', 'smarterp_score', 'smarterp_tier'); limit = 1 })
if (-not $found) { throw "No lead found matching '$Lead'" }
$record = $found[0]
if (-not $record.email_from) { throw "Lead '$($record.name)' has no email address to reply from" }
Write-Host "Before: $($record.name) -> $($record.smarterp_score) $($record.smarterp_tier)"

# Address the reply to our own user, so Odoo doesn't suggest a made-up inbox as a recipient later.
$ourEmail = (Invoke-OdooRpc 'object' 'execute_kw' @($Database, $uid, $Password, 'res.users', 'read',
    @(, @($uid)), @{ fields = @('email') }))[0].email

$email = @"
From: $($record.email_from)
To: $ourEmail
Subject: Re: $($record.name)
Message-ID: <demo-reply-$([guid]::NewGuid())@smarterp-demo.example>
Date: $((Get-Date).ToUniversalTime().ToString('r'))
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<p>$([System.Net.WebUtility]::HtmlEncode($Body))</p>
"@

Invoke-OdooRpc 'object' 'execute_kw' @($Database, $uid, $Password, 'mail.thread', 'message_process',
    @('crm.lead', $email), @{ thread_id = $record.id }) | Out-Null

$after = (Invoke-OdooRpc 'object' 'execute_kw' @($Database, $uid, $Password, 'crm.lead', 'read',
    @(, @($record.id)), @{ fields = @('smarterp_score', 'smarterp_tier', 'smarterp_confidence') }))[0]
Write-Host "After:  $($record.name) -> $($after.smarterp_score) $($after.smarterp_tier) ($($after.smarterp_confidence)% confidence)"
