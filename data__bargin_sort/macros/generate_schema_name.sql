{#
    Use the model's +schema config verbatim as the schema name.

    dbt's default prefixes the target schema, which would turn `silver` into
    `public_silver`. The medallion layers are named schemas in their own right,
    so a custom schema replaces the target schema rather than extending it.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
