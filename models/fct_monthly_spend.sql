{{ config(materialized='view') }}

select
    m.month_start as reporting_month,
    b.cost_centre_id,
    c.centre_name as cost_centre_name,
    b.account_id,
    cast(a.account_no as string) as account_no,
    a.account_label,
    cast(b.actual_amount as numeric) as actual_amount,
    cast(b.budget_amount as numeric) as budget_amount,
    cast(b.actual_amount as numeric) - cast(b.budget_amount as numeric) as variance_amount
from {{ source('finance_raw', 'budget_lines') }} as b
left join {{ source('finance_raw', 'months') }} as m on b.month_id = m.id
left join {{ source('finance_raw', 'cost_centers') }} as c on b.cost_centre_id = c.id
left join {{ source('finance_raw', 'accounts') }} as a on b.account_id = a.id
