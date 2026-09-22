#include "duckdb/function/table/system_catalog_functions.hpp"

#include "duckdb/catalog/catalog.hpp"
#include "duckdb/common/serializer/deserializer.hpp"
#include "duckdb/common/serializer/serializer.hpp"
#include "duckdb/main/attached_database.hpp"
#include "duckdb/main/database_manager.hpp"
#include "duckdb/planner/expression/bound_columnref_expression.hpp"
#include "duckdb/planner/expression/bound_comparison_expression.hpp"
#include "duckdb/planner/expression/bound_constant_expression.hpp"
#include "duckdb/planner/operator/logical_get.hpp"

namespace duckdb {

unique_ptr<FunctionData> SystemCatalogScanBindData::Copy() const {
	auto result = make_uniq<SystemCatalogScanBindData>();
	result->catalog = catalog;
	return std::move(result);
}

bool SystemCatalogScanBindData::Equals(const FunctionData &other_p) const {
	auto &other = other_p.Cast<SystemCatalogScanBindData>();
	return catalog == other.catalog;
}

unique_ptr<FunctionData> SystemCatalogScanFunction::Bind() {
	return make_uniq<SystemCatalogScanBindData>();
}

vector<reference<SchemaCatalogEntry>>
SystemCatalogScanFunction::GetSchemas(ClientContext &context, optional_ptr<const FunctionData> bind_data) {
	if (!bind_data) {
		return Catalog::GetAllSchemas(context);
	}
	auto &data = bind_data->Cast<SystemCatalogScanBindData>();
	if (data.catalog.empty()) {
		return Catalog::GetAllSchemas(context);
	}

	auto database = DatabaseManager::Get(context).GetDatabase(context, Identifier(data.catalog));
	if (!database || database->GetName().GetIdentifierName() != data.catalog ||
	    database->GetVisibility() == AttachVisibility::HIDDEN) {
		return {};
	}
	return database->GetCatalog().GetSchemas(context);
}

static bool IsDatabaseNameColumn(const LogicalGet &get, const BoundColumnRefExpression &ref) {
	if (ref.Binding().table_index != get.table_index) {
		return false;
	}
	const auto &column_ids = get.GetColumnIds();
	if (ref.Binding().column_index < column_ids.size()) {
		const auto &col_idx = column_ids[ref.Binding().column_index];
		if (col_idx.HasPrimaryIndex()) {
			return get.GetColumnName(col_idx) == SystemCatalogScanFunction::DATABASE_NAME_COLUMN;
		}
	}
	return ref.GetAlias() == SystemCatalogScanFunction::DATABASE_NAME_COLUMN;
}

static bool TryExtractDatabaseNameEquality(const LogicalGet &get, const Expression &expr, string &catalog) {
	if (!BoundComparisonExpression::IsComparison(expr)) {
		return false;
	}
	auto &comp = expr.Cast<BoundFunctionExpression>();
	if (comp.GetExpressionType() != ExpressionType::COMPARE_EQUAL) {
		return false;
	}

	auto &left = BoundComparisonExpression::Left(comp);
	auto &right = BoundComparisonExpression::Right(comp);
	const BoundColumnRefExpression *column_ref = nullptr;
	const BoundConstantExpression *constant = nullptr;
	if (left.GetExpressionClass() == ExpressionClass::BOUND_COLUMN_REF &&
	    right.GetExpressionClass() == ExpressionClass::BOUND_CONSTANT) {
		column_ref = &left.Cast<BoundColumnRefExpression>();
		constant = &right.Cast<BoundConstantExpression>();
	} else if (left.GetExpressionClass() == ExpressionClass::BOUND_CONSTANT &&
	           right.GetExpressionClass() == ExpressionClass::BOUND_COLUMN_REF) {
		column_ref = &right.Cast<BoundColumnRefExpression>();
		constant = &left.Cast<BoundConstantExpression>();
	} else {
		return false;
	}

	if (!IsDatabaseNameColumn(get, *column_ref) || constant->GetValue().IsNull() ||
	    constant->GetValue().type().id() != LogicalTypeId::VARCHAR) {
		return false;
	}
	catalog = StringValue::Get(constant->GetValue());
	return !catalog.empty();
}

void SystemCatalogScanFunction::PushdownComplexFilter(ClientContext &, LogicalGet &get, FunctionData *bind_data,
                                                      vector<unique_ptr<Expression>> &filters) {
	if (!bind_data) {
		return;
	}
	auto &data = bind_data->Cast<SystemCatalogScanBindData>();
	for (idx_t i = 0; i < filters.size(); i++) {
		string catalog;
		if (!TryExtractDatabaseNameEquality(get, *filters[i], catalog)) {
			continue;
		}
		if (!data.catalog.empty() && data.catalog != catalog) {
			continue;
		}
		data.catalog = std::move(catalog);
		filters.erase_at(i);
		i--;
	}
}

InsertionOrderPreservingMap<string> SystemCatalogScanFunction::ToString(TableFunctionToStringInput &input) {
	InsertionOrderPreservingMap<string> result;
	if (!input.bind_data) {
		return result;
	}
	auto &data = input.bind_data->Cast<SystemCatalogScanBindData>();
	if (!data.catalog.empty()) {
		result["Catalog"] = data.catalog;
	}
	return result;
}

void SystemCatalogScanFunction::Serialize(Serializer &serializer, const optional_ptr<FunctionData> bind_data,
                                          const TableFunction &) {
	string catalog;
	if (bind_data) {
		catalog = bind_data->Cast<SystemCatalogScanBindData>().catalog;
	}
	serializer.WritePropertyWithDefault<string>(100, "catalog", catalog);
}

unique_ptr<FunctionData> SystemCatalogScanFunction::Deserialize(Deserializer &deserializer, TableFunction &) {
	auto result = make_uniq<SystemCatalogScanBindData>();
	deserializer.ReadPropertyWithDefault<string>(100, "catalog", result->catalog);
	return std::move(result);
}

void SystemCatalogScanFunction::Register(TableFunction &function) {
	function.pushdown_complex_filter = PushdownComplexFilter;
	function.to_string = ToString;
	function.serialize = Serialize;
	function.deserialize = Deserialize;
}

} // namespace duckdb
