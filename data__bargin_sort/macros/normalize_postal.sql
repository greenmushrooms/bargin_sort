{#
    Reduce a raw postal string to the key used by the postal_centroids seed.

    GeoNames only publishes Canadian postal data at FSA level (the first three
    characters), while US data is full five-digit ZIPs. HiBid stores both in
    one free-text field, spaced inconsistently: 'L5L5Z9', 'M1P 4P8', '25526'.
#}
{% macro normalize_postal(column) -%}
    case
        when upper(regexp_replace({{ column }}, '[^A-Za-z0-9]', '', 'g')) ~ '^[A-Z][0-9][A-Z]'
            then left(upper(regexp_replace({{ column }}, '[^A-Za-z0-9]', '', 'g')), 3)
        when regexp_replace({{ column }}, '[^0-9]', '', 'g') ~ '^[0-9]{5}'
            then left(regexp_replace({{ column }}, '[^0-9]', '', 'g'), 5)
    end
{%- endmacro %}


{# Country implied by the shape of the postal code, matching the seed's keys. #}
{% macro postal_country(column) -%}
    case
        when upper(regexp_replace({{ column }}, '[^A-Za-z0-9]', '', 'g')) ~ '^[A-Z][0-9][A-Z]' then 'CA'
        when regexp_replace({{ column }}, '[^0-9]', '', 'g') ~ '^[0-9]{5}' then 'US'
    end
{%- endmacro %}
