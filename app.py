import json
from typing import Optional, List

from fastapi import FastAPI, Query
from sqlalchemy import create_engine, Table, MetaData, distinct, func, literal
from sqlalchemy.sql.expression import desc, select
from starlette.middleware.cors import CORSMiddleware

# Load configuration from JSON file
with open("./config.json", "r") as config_file:
    config = json.load(config_file)

DATABASE_URL = config["db_uri"]

# Maximum number of connections and maximum overflow
max_connections = 100
max_overflow = 50

# Create the engine with connection pooling
engine = create_engine(
    DATABASE_URL,
    pool_size=max_connections,
    max_overflow=max_overflow
)

metadata = MetaData()

non_mm_ledger_updates = Table("non_mm_ledger_updates", metadata, autoload_with=engine)
non_mm_trades_cache = Table("non_mm_trades_cache", metadata, autoload_with=engine)
non_mm_ledger_updates_cache = Table(
    "non_mm_ledger_updates_cache", metadata, autoload_with=engine
)
liquidations_cache = Table("liquidations_cache", metadata, autoload_with=engine)
account_values_cache = Table('account_values_cache', metadata, autoload_with=engine)
funding_cache = Table('funding_cache', metadata, autoload_with=engine)
asset_ctxs_cache = Table('asset_ctxs_cache', metadata, autoload_with=engine)
market_data_cache = Table("market_data_cache", metadata, autoload_with=engine)

hlp_vault_addresses = [
    "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303",
    "0x010461c14e146ac35fe42271bdc1134ee31c703a",
    "0x31ca8395cf837de08b24da3f660e77761dfb974b",
    "0x63c621a33714ec48660e32f2374895c8026a3a00"
]

app = FastAPI()

origins = config["origins"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def apply_filters(query, table, start_date, end_date, coins: Optional[List[str]] = None):
    if start_date:
        query = query.where(table.c.time >= start_date)
    if end_date:
        query = query.where(table.c.time <= end_date)
    if coins:
        query = query.where(table.c.coin.in_(coins))
    return query


@app.get("/hyperliquid/total_users")
async def get_total_users(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    coins: Optional[List[str]] = Query(None),
):
    with engine.begin() as connection:
        query = select(func.count(distinct(non_mm_trades_cache.c.user)).label("total_users"))
        query = apply_filters(query, non_mm_trades_cache, start_date, end_date, coins)
        result = connection.execute(query)
        return {"total_users": result.scalar()}


@app.get("/hyperliquid/total_usd_volume")
async def get_total_volume(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    coins: Optional[List[str]] = Query(None),
):
    with engine.begin() as connection:
        query = select(
            func.sum(
                non_mm_trades_cache.c.usd_volume
            ).label("total_usd_volume")
        ).select_from(non_mm_trades_cache)
        query = apply_filters(query, non_mm_trades_cache, start_date, end_date, coins)
        result = connection.execute(query)
        return {"total_usd_volume": result.scalar()}


@app.get("/hyperliquid/total_deposits")
async def get_total_deposits(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        query = select(
            func.sum(non_mm_ledger_updates.c.delta_usd).label(
                "total_deposits"
            )
        ).where(non_mm_ledger_updates.c.delta_usd > 0).select_from(non_mm_ledger_updates)
        query = apply_filters(query, non_mm_ledger_updates, start_date, end_date)
        result = connection.execute(query)
        return {"total_deposits": result.scalar()}


@app.get("/hyperliquid/total_withdrawals")
async def get_total_withdrawals(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        query = select(
            func.sum(non_mm_ledger_updates.c.delta_usd).label(
                "total_withdrawals"
            )
        ).where(non_mm_ledger_updates.c.delta_usd < 0).select_from(non_mm_ledger_updates)
        query = apply_filters(query, non_mm_ledger_updates, start_date, end_date)
        result = connection.execute(query)
        return {"total_withdrawals": result.scalar()}


@app.get("/hyperliquid/total_notional_liquidated")
async def get_total_notional_liquidated(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        query = select(
            func.sum(liquidations_cache.c.sum_liquidated_ntl_pos).label(
                "total_notional_liquidated"
            )
        ).select_from(liquidations_cache)
        query = apply_filters(query, liquidations_cache, start_date, end_date)
        result = connection.execute(query)
        return {"total_notional_liquidated": result.scalar()}


def get_cumulative_chart_data(table, column, start_date, end_date, coins):
    with engine.begin() as connection:
        # First, create a subquery that groups by date and sums the column
        subquery = select(
            table.c.time,
            func.sum(table.c[column]).label(column),
        ).group_by(table.c.time)
        subquery = apply_filters(subquery, table, start_date, end_date, coins)
        subquery = subquery.alias('subquery')

        # Then, select from the subquery and calculate the cumulative sum
        query = select(
            subquery.c.time,
            func.sum(subquery.c[column])
            .over(order_by=subquery.c.time)
            .label(f"cumulative_{column}"),
        ).order_by(subquery.c.time)

        result = connection.execute(query)
        chart_data = [
            {"time": row.time, f"cumulative_{column}": row[f"cumulative_{column}"]}
            for row in result
        ]
        return chart_data


@app.get("/hyperliquid/cumulative_usd_volume")
async def get_cumulative_usd_volume(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    coins: Optional[List[str]] = Query(None),
):
    return {
        "chart_data": get_cumulative_chart_data(
            non_mm_trades_cache, "usd_volume", start_date, end_date, coins
        )
    }


@app.get("/hyperliquid/daily_usd_volume")
async def get_daily_usd_volume(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    coins: Optional[List[str]] = Query(None),
):
    with engine.begin() as connection:
        query = (
            select(
                non_mm_trades_cache.c.time,
                func.sum(non_mm_trades_cache.c.usd_volume).label("daily_usd_volume"),
            )
            .group_by(non_mm_trades_cache.c.time)
            .order_by(non_mm_trades_cache.c.time)
        )
        query = apply_filters(query, non_mm_trades_cache, start_date, end_date, coins)
        result = connection.execute(query)
        chart_data = [
            {"time": row.time, "daily_usd_volume": row.daily_usd_volume} for row in result
        ]
        return {"chart_data": chart_data}


@app.get("/hyperliquid/daily_usd_volume_by_coin")
async def get_daily_usd_volume_by_coin(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        query = (
            select(
                non_mm_trades_cache.c.time,
                non_mm_trades_cache.c.coin,
                func.sum(non_mm_trades_cache.c.usd_volume).label("daily_usd_volume"),
            )
            .group_by(non_mm_trades_cache.c.time, non_mm_trades_cache.c.coin)
            .order_by(non_mm_trades_cache.c.time)
        )
        query = apply_filters(query, non_mm_trades_cache, start_date, end_date)
        result = connection.execute(query)
        chart_data = [
            {"time": row.time, "coin": row.coin, "daily_usd_volume": row.daily_usd_volume}
            for row in result
        ]
        return {"chart_data": chart_data}


@app.get("/hyperliquid/daily_usd_volume_by_crossed")
async def get_daily_usd_volume_by_crossed(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        query = (
            select(
                non_mm_trades_cache.c.time,
                non_mm_trades_cache.c.crossed,
                func.sum(non_mm_trades_cache.c.usd_volume).label("daily_usd_volume"),
            )
            .group_by(non_mm_trades_cache.c.time, non_mm_trades_cache.c.crossed)
            .order_by(non_mm_trades_cache.c.time)
        )
        query = apply_filters(query, non_mm_trades_cache, start_date, end_date)
        result = connection.execute(query)
        chart_data = [
            {"time": row.time, "crossed": row.crossed, "daily_usd_volume": row.daily_usd_volume}
            for row in result
        ]
        return {"chart_data": chart_data}


# TODO figure out other
@app.get("/hyperliquid/daily_usd_volume_by_user")
async def get_daily_usd_volume_by_user(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        subquery = (
            select(
                non_mm_trades_cache.c.time,
                non_mm_trades_cache.c.user,
                func.sum(non_mm_trades_cache.c.usd_volume).label("total_usd_volume")
            )
            .group_by(non_mm_trades_cache.c.time, non_mm_trades_cache.c.user)
            .alias("daily_volume")
        )

        inner_query = (
            select(
                subquery.c.time.label("date"),
                subquery.c.user,
                subquery.c.total_usd_volume,
                func.rank().over(
                    partition_by=subquery.c.time,
                    order_by=subquery.c.total_usd_volume.desc()
                ).label("user_rank")
            )
            .select_from(subquery)
            .where(subquery.c.time.between(start_date, end_date))  # Apply date filtering in the subquery
        )

        query = (
            select(
                inner_query.c.date,
                inner_query.c.user,
                inner_query.c.total_usd_volume
            )
            .select_from(inner_query)
            .where(inner_query.c.user_rank <= 10)  # Apply user rank filtering
            .order_by(inner_query.c.date, inner_query.c.total_usd_volume.desc())
        )

        # Calculate the sum of USD volume for the top 10 users per day
        top_10_usd_volume_subquery = (
            select(
                inner_query.c.date.label("date"),
                func.sum(inner_query.c.total_usd_volume).label("top_10_usd_volume")
            )
            .select_from(inner_query)
            .where(inner_query.c.user_rank <= 10)
            .group_by(inner_query.c.date)
            .alias("top_10_usd_volume")
        )

        # Add the "Other" row per day
        other_subquery = (
            select(
                inner_query.c.date,
                literal("Other").label("user"),
                (top_10_usd_volume_subquery.c.top_10_usd_volume - func.coalesce(func.sum(inner_query.c.total_usd_volume), 0)).label("total_usd_volume")
            )
            .select_from(inner_query)
            .join(top_10_usd_volume_subquery, inner_query.c.date == top_10_usd_volume_subquery.c.date)
            .where(inner_query.c.user_rank > 10)  # Filter out the top 10 users
            .group_by(inner_query.c.date, top_10_usd_volume_subquery.c.top_10_usd_volume)
        )

        query = query.union(other_subquery).order_by(query.c.date)

        result = connection.execute(query)
        chart_data = [
            {"date": row.date, "user": row.user, "daily_usd_volume": row.total_usd_volume} for row in result
        ]
        return {"chart_data": chart_data}


@app.get("/hyperliquid/cumulative_trades")
async def get_cumulative_trades(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    coins: Optional[List[str]] = Query(None),
):
    return {
        "chart_data": get_cumulative_chart_data(
            non_mm_trades_cache, "group_count", start_date, end_date, coins
        )
    }


@app.get("/hyperliquid/daily_trades")
async def get_daily_trades(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    coins: Optional[List[str]] = Query(None),
):
    with engine.begin() as connection:
        query = (
            select(
                non_mm_trades_cache.c.time,
                func.sum(non_mm_trades_cache.c.group_count).label("daily_trades"),
            )
            .group_by(non_mm_trades_cache.c.time)
            .order_by(non_mm_trades_cache.c.time)
        )
        query = apply_filters(query, non_mm_trades_cache, start_date, end_date, coins)
        result = connection.execute(query)
        chart_data = [
            {"date": row.time, "daily_trades": row.daily_trades} for row in result
        ]
        return {"chart_data": chart_data}


@app.get("/hyperliquid/daily_trades_by_coin")
async def get_daily_trades_by_coin(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        query = (
            select(
                non_mm_trades_cache.c.time,
                non_mm_trades_cache.c.coin,
                func.sum(non_mm_trades_cache.c.group_count).label("daily_trades"),
            )
            .group_by(non_mm_trades_cache.c.time, non_mm_trades_cache.c.coin)
            .order_by(non_mm_trades_cache.c.time)
        )
        query = apply_filters(query, non_mm_trades_cache, start_date, end_date)
        result = connection.execute(query)
        chart_data = [
            {"time": row.time, "coin": row.coin, "daily_trades": row.daily_trades}
            for row in result
        ]
        return {"chart_data": chart_data}


@app.get("/hyperliquid/daily_trades_by_crossed")
async def get_daily_trades_by_crossed(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        query = (
            select(
                non_mm_trades_cache.c.time,
                non_mm_trades_cache.c.crossed,
                func.sum(non_mm_trades_cache.c.group_count).label("daily_trades"),
            )
            .group_by(non_mm_trades_cache.c.time, non_mm_trades_cache.c.crossed)
            .order_by(non_mm_trades_cache.c.time)
        )
        query = apply_filters(query, non_mm_trades_cache, start_date, end_date)
        result = connection.execute(query)
        chart_data = [
            {"time": row.time, "crossed": row.crossed, "daily_trades": row.daily_trades}
            for row in result
        ]
        return {"chart_data": chart_data}


# TODO figure out other
@app.get("/hyperliquid/daily_trades_by_user")
async def get_daily_trades_by_user(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        subquery = (
            select(
                non_mm_trades_cache.c.time,
                non_mm_trades_cache.c.user,
                func.sum(non_mm_trades_cache.c.group_count).label("total_group_count")
            )
            .group_by(non_mm_trades_cache.c.time, non_mm_trades_cache.c.user)
            .alias("daily_group_count")
        )

        inner_query = (
            select(
                subquery.c.time.label("date"),
                subquery.c.user,
                subquery.c.total_group_count,
                func.rank().over(
                    partition_by=subquery.c.time,
                    order_by=subquery.c.total_group_count.desc()
                ).label("user_rank")
            )
            .select_from(subquery)
            .where(subquery.c.time.between(start_date, end_date))  # Apply date filtering in the subquery
        )

        query = (
            select(
                inner_query.c.date,
                inner_query.c.user,
                inner_query.c.total_group_count
            )
            .select_from(inner_query)
            .where(inner_query.c.user_rank <= 10)  # Apply user rank filtering
            .order_by(inner_query.c.date, inner_query.c.total_group_count.desc())
        )

        # Calculate the sum of group_count for the top 10 users per day
        top_10_group_count_subquery = (
            select(
                inner_query.c.date.label("date"),
                func.sum(inner_query.c.total_group_count).label("top_10_group_count")
            )
            .select_from(inner_query)
            .where(inner_query.c.user_rank <= 10)
            .group_by(inner_query.c.date)
            .alias("top_10_group_count")
        )

        # Add the "Other" row per day
        other_subquery = (
            select(
                inner_query.c.date,
                literal("Other").label("user"),
                (top_10_group_count_subquery.c.top_10_group_count - func.coalesce(
                    func.sum(inner_query.c.total_group_count), 0)).label("total_group_count")
            )
            .select_from(inner_query)
            .join(top_10_group_count_subquery, inner_query.c.date == top_10_group_count_subquery.c.date)
            .where(inner_query.c.user_rank > 10)  # Filter out the top 10 users
            .group_by(inner_query.c.date, top_10_group_count_subquery.c.top_10_group_count)
        )

        query = query.union(other_subquery).order_by(query.c.date)

        result = connection.execute(query)
        chart_data = [
            {"date": row.date, "user": row.user, "daily_group_count": row.total_group_count} for row in result
        ]
        return {"chart_data": chart_data}


def calculate_cumulative_pnl(chart_data):
    chart_data.sort(key=lambda x: x["time"])
    cumulative_pnl = 0
    cumulative_data = []
    for data in chart_data:
        cumulative_pnl += data["pnl"]
        cumulative_data.append({"time": data["time"], "cumulative_pnl": cumulative_pnl})
    return cumulative_data


@app.get("/hyperliquid/cumulative_user_pnl")
async def get_cumulative_user_pnl(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        # Exclude vault addresses and filter on 'is_vault=false'
        subquery = (
            select([
                account_values_cache.c.time,
                func.sum(
                    account_values_cache.c.sum_account_value
                    - account_values_cache.c.sum_cum_ledger
                ).label("daily_pnl"),
            ])
            .where(account_values_cache.c.user.notin_(hlp_vault_addresses))
            .where(account_values_cache.c.is_vault == False)
            .group_by(account_values_cache.c.time)
        ).alias('sub')

        query = (
            select([
                subquery.c.time,
                func.sum(subquery.c.daily_pnl).over(order_by=subquery.c.time).label("cumulative_pnl"),
            ])
        )

        query = apply_filters(query, account_values_cache, start_date, end_date, None)

        results = connection.execute(query)
        chart_data = [
            {"time": row[0], "cumulative_pnl": row[1]} for row in results
        ]
        return chart_data


@app.get("/hyperliquid/user_pnl")
async def get_user_pnl(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        # Exclude vault addresses and filter on 'is_vault=false'
        query = (
            select([
                account_values_cache.c.time,
                func.sum(
                    account_values_cache.c.sum_account_value
                    - account_values_cache.c.sum_cum_ledger
                ).label("total_pnl"),
            ])
            .where(account_values_cache.c.user.notin_(hlp_vault_addresses))
            .where(account_values_cache.c.is_vault == False)
            .group_by(account_values_cache.c.time)
        )

        query = apply_filters(query, account_values_cache, start_date, end_date, None)

        results = connection.execute(query)
        chart_data = [
            {"time": row[0], "total_pnl": row[1]} for row in results
        ]
        return chart_data


@app.get("/hyperliquid/hlp_liquidator_pnl")
async def get_hlp_liquidator_pnl(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        # Include only vault addresses
        query = (
            select([
                account_values_cache.c.time,
                func.sum(
                    account_values_cache.c.sum_account_value
                    - account_values_cache.c.sum_cum_ledger
                ).label("total_pnl"),
            ])
            .where(account_values_cache.c.user.in_(hlp_vault_addresses))
            .group_by(account_values_cache.c.time)
        )

        query = apply_filters(query, account_values_cache, start_date, end_date, None)

        results = connection.execute(query)
        chart_data = [
            {"time": row[0], "total_pnl": row[1]} for row in results
        ]
        return chart_data


@app.get("/hyperliquid/cumulative_hlp_liquidator_pnl")
async def get_cumulative_user_pnl(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        # Include only vault addresses
        subquery = (
            select([
                account_values_cache.c.time,
                func.sum(
                    account_values_cache.c.sum_account_value
                    - account_values_cache.c.sum_cum_ledger
                ).label("daily_pnl"),
            ])
            .where(account_values_cache.c.user.in_(hlp_vault_addresses))
            .group_by(account_values_cache.c.time)
        ).alias('sub')

        query = (
            select([
                subquery.c.time,
                func.sum(subquery.c.daily_pnl).over(order_by=subquery.c.time).label("cumulative_pnl"),
            ])
        )

        query = apply_filters(query, account_values_cache, start_date, end_date, None)

        results = connection.execute(query)
        chart_data = [
            {"time": row[0], "cumulative_pnl": row[1]} for row in results
        ]
        return chart_data


@app.get("/hyperliquid/cumulative_liquidated_notional")
async def get_cumulative_liquidated_notional(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    return {
        "chart_data": get_cumulative_chart_data(
            liquidations_cache, "sum_liquidated_ntl_pos", start_date, end_date, None
        )
    }


@app.get("/hyperliquid/daily_notional_liquidated_total")
async def get_daily_notional_liquidated_total(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        query = (
            select(
                liquidations_cache.c.time,
                func.sum(liquidations_cache.c.sum_liquidated_ntl_pos).label("daily_notional_liquidated"),
            )
            .group_by(liquidations_cache.c.time)
            .order_by(liquidations_cache.c.time)
        )
        query = apply_filters(query, liquidations_cache, start_date, end_date)
        result = connection.execute(query)
        chart_data = [
            {"time": row.time, "daily_notional_liquidated": row.daily_notional_liquidated}
            for row in result
        ]
        return {"chart_data": chart_data}


@app.get("/hyperliquid/daily_notional_liquidated_by_leverage_type")
async def get_daily_notional_liquidated_by_leverage_type(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        query = (
            select(
                liquidations_cache.c.time,
                liquidations_cache.c.leverage_type,
                func.sum(liquidations_cache.c.sum_liquidated_ntl_pos).label("daily_notional_liquidated"),
            )
            .group_by(liquidations_cache.c.time, liquidations_cache.c.leverage_type)
            .order_by(liquidations_cache.c.time)
        )
        query = apply_filters(query, liquidations_cache, start_date, end_date)
        result = connection.execute(query)
        chart_data = [
            {
                "time": row.time,
                "leverage_type": row.leverage_type,
                "daily_notional_liquidated": row.daily_notional_liquidated,
            }
            for row in result
        ]
        return {"chart_data": chart_data}


@app.get("/hyperliquid/daily_unique_users")
async def get_daily_unique_users(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    coins: Optional[List[str]] = Query(None),
):
    with engine.begin() as connection:
        query = (
            select(
                non_mm_trades_cache.c.time,
                func.count(distinct(non_mm_trades_cache.c.user)).label("daily_unique_users"),
            )
            .group_by(non_mm_trades_cache.c.time)
            .order_by(non_mm_trades_cache.c.time)
        )
        query = apply_filters(query, non_mm_trades_cache, start_date, end_date, coins)
        result = connection.execute(query)
        chart_data = [
            {"time": row.time, "daily_unique_users": row.daily_unique_users}
            for row in result
        ]
        return {"chart_data": chart_data}


@app.get("/hyperliquid/daily_unique_users_by_coin")
async def get_daily_unique_users_by_coin(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        # Get the total unique users per day
        total_users_query = (
            select(
                non_mm_trades_cache.c.time,
                func.count(distinct(non_mm_trades_cache.c.user)).label("total_unique_users"),
            )
            .group_by(non_mm_trades_cache.c.time)
            .order_by(non_mm_trades_cache.c.time)
        )
        total_users_query = apply_filters(total_users_query, non_mm_trades_cache, start_date, end_date)
        total_users_result = connection.execute(total_users_query)
        total_users_data = {
            row.time: row.total_unique_users
            for row in total_users_result
        }

        # Get the daily unique users by coin
        query = (
            select(
                non_mm_trades_cache.c.time,
                non_mm_trades_cache.c.coin,
                func.count(distinct(non_mm_trades_cache.c.user)).label("daily_unique_users"),
            )
            .group_by(non_mm_trades_cache.c.time, non_mm_trades_cache.c.coin)
            .order_by(non_mm_trades_cache.c.time)
        )
        query = apply_filters(query, non_mm_trades_cache, start_date, end_date)
        result = connection.execute(query)

        chart_data = []
        for row in result:
            time = row.time
            coin = row.coin
            daily_unique_users = row.daily_unique_users
            total_unique_users = total_users_data.get(time, 1)  # Default to 1 to avoid division by zero

            percentage_of_total_users = (daily_unique_users / total_unique_users) * 100
            chart_data.append({
                "time": time,
                "coin": coin,
                "daily_unique_users": daily_unique_users,
                "percentage_of_total_users": percentage_of_total_users,
            })

        return {"chart_data": chart_data}


@app.get("/hyperliquid/open_interest")
async def get_open_interest(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        coins: Optional[List[str]] = None,
):
    with engine.begin() as connection:
        query = (
            select(
                asset_ctxs_cache.c.time,
                asset_ctxs_cache.c.coin,
                func.sum(asset_ctxs_cache.c.sum_open_interest).label("sum_open_interest"),
            )
            .group_by(asset_ctxs_cache.c.time, asset_ctxs_cache.c.coin)  # Include asset_ctxs_cache.coin in the GROUP BY clause
            .order_by(asset_ctxs_cache.c.time)
        )

        query = apply_filters(query, asset_ctxs_cache, start_date, end_date, coins)

        result = connection.execute(query)
        chart_data = [
            {"time": row.time, "coin": row.coin, "open_interest": row.sum_open_interest}
            for row in result
        ]
        return {"chart_data": chart_data}


@app.get("/hyperliquid/funding_rate")
async def get_funding_rate(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        coins: Optional[List[str]] = Query(None),
):
    with engine.begin() as connection:
        query = (
            select(
                non_mm_trades_cache.c.time,
                funding_cache.c.coin,
                func.sum(funding_cache.c.sum_funding).label("sum_funding"),
            )
            .group_by(non_mm_trades_cache.c.time, funding_cache.c.coin)
            .order_by(non_mm_trades_cache.c.time)
        )
        query = apply_filters(query, non_mm_trades_cache, start_date, end_date, coins)
        result = connection.execute(query)
        chart_data = [
            {"time": row.time, "coin": row.coin, "sum_funding": row.sum_funding * 365}
            for row in result
        ]
        return {"chart_data": chart_data}


@app.get("/hyperliquid/cumulative_unique_users")
async def get_cumulative_unique_users(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        coins: Optional[List[str]] = Query(None),
):
    with engine.begin() as connection:
        # Apply filters to non_mm_trades_cache
        filtered_trades = non_mm_trades_cache.select()
        filtered_trades = apply_filters(filtered_trades, non_mm_trades_cache, start_date, end_date, coins)

        # Create subquery to get the first trade date for each user
        subquery = select([
            filtered_trades.c.user,
            func.min(filtered_trades.c.time).label('first_trade_date')
        ]).group_by(filtered_trades.c.user)

        # Convert to alias for later usage
        user_first_trade_dates = subquery.alias('user_first_trade_dates')

        # Now select the date and count distinct users by date
        query = select([
            user_first_trade_dates.c.first_trade_date.label('date'),
            func.count(user_first_trade_dates.c.user).label('daily_unique_users')
        ]).group_by(user_first_trade_dates.c.first_trade_date)

        # Then select date, daily_unique_users, and the cumulative count of unique users
        final_query = select([
            query.c.date,
            query.c.daily_unique_users,
            func.sum(query.c.daily_unique_users).over(order_by=query.c.date).label('cumulative_unique_users')
        ])

        # Execute the final query
        result = connection.execute(final_query)

        # Convert result to JSON-serializable format
        chart_data = [{"date": row.date, "daily_unique_users": row.daily_unique_users,
                       "cumulative_unique_users": row.cumulative_unique_users} for row in result]

        return {"chart_data": chart_data}


@app.get("/hyperliquid/cumulative_inflow")
async def get_cumulative_inflow(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        base_query = (
            select(
                non_mm_ledger_updates_cache.c.time,
                func.sum(non_mm_ledger_updates_cache.c.sum_delta_usd).label("inflow_per_day")
            )
            .group_by(non_mm_ledger_updates_cache.c.time)
            .order_by(non_mm_ledger_updates_cache.c.time)
        )

        filtered_base_query = apply_filters(base_query, non_mm_ledger_updates_cache, start_date, end_date)

        query = filtered_base_query.alias('inflows_per_day')

        cumulative_query = (
            select(
                query.c.time,
                func.sum(query.c.inflow_per_day)
                .over(order_by=query.c.time)
                .label("cumulative_inflow"),
            )
            .order_by(query.c.time)
        )

        result = connection.execute(cumulative_query)
        chart_data = [
            {"time": row.time, "cumulative_inflow": row.cumulative_inflow}
            for row in result
        ]
        return {"chart_data": chart_data}


@app.get("/hyperliquid/daily_inflow")
async def get_daily_inflow(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with engine.begin() as connection:
        base_query = (
            select(
                non_mm_ledger_updates_cache.c.time,
                func.sum(non_mm_ledger_updates_cache.c.sum_delta_usd).label("inflow_per_day")
            )
            .group_by(non_mm_ledger_updates_cache.c.time)
            .order_by(non_mm_ledger_updates_cache.c.time)
        )

        filtered_base_query = apply_filters(base_query, non_mm_ledger_updates_cache, start_date, end_date)

        query = (
            select(
                filtered_base_query.c.time.label("time"),
                filtered_base_query.c.inflow_per_day.label("inflow")
            )
            .alias('inflows_per_day')
        )

        result = connection.execute(query)
        chart_data = [
            {"time": row.time, "inflow": row.inflow}
            for row in result
        ]
        return {"chart_data": chart_data}


@app.get("/hyperliquid/liquidity_per_symbol")
async def get_liquidity_per_symbol(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    notional_amounts: Optional[List[int]] = Query([1000, 3000, 10000]),
):
    with engine.begin() as connection:
        data = {}
        for notional in notional_amounts:
            query = (
                select(
                    market_data_cache.c.time,
                    market_data_cache.c.coin,
                    func.avg(market_data_cache.c.median_liquidity / notional).label('average_liquidity_percentage')
                )
                .group_by(market_data_cache.c.time, market_data_cache.c.coin)
                .order_by(market_data_cache.c.time, market_data_cache.c.coin)
            )
            query = apply_filters(query, market_data_cache, start_date, end_date)

            result = connection.execute(query)

            # Collect data
            for row in result:
                time, coin, average_liquidity_percentage = row
                if coin not in data:
                    data[coin] = {}
                if notional not in data[coin]:
                    data[coin][notional] = []
                data[coin][notional].append({"time": time, "average_liquidity_percentage": average_liquidity_percentage})

    return data


def get_table_data(table, group_by_column, sum_column, start_date, end_date, coins, limit):
    with engine.begin() as connection:
        query = (
            select(table.c[group_by_column], func.sum(table.c[sum_column]).label(sum_column))
            .group_by(table.c[group_by_column])
            .order_by(desc(sum_column))
            .limit(limit)
        )
        query = apply_filters(query, table, start_date, end_date, coins)
        result = connection.execute(query)
        table_data = [
            {"name": row[group_by_column], "value": row[sum_column]} for row in result
        ]
        return table_data


@app.get("/hyperliquid/largest_users_by_usd_volume")
async def get_largest_users_by_usd_volume(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    coins: Optional[List[str]] = Query(None),
):
    return {
        "table_data": get_table_data(
            non_mm_trades_cache, "user", "usd_volume", start_date, end_date, coins, 10
        )
    }


@app.get("/hyperliquid/largest_user_depositors")
async def get_largest_user_depositors(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    return {
        "table_data": get_table_data(
            non_mm_ledger_updates_cache, "user", "sum_delta_usd", start_date, end_date, None, 10
        )
    }


@app.get("/hyperliquid/largest_liquidated_notional_by_user")
async def get_largest_liquidated_notional_by_user(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    return {
        "table_data": get_table_data(
            liquidations_cache, "user", "sum_liquidated_account_value", start_date, end_date, None, 10
        )
    }


@app.get("/hyperliquid/largest_user_trade_count")
async def get_largest_user_trade_count(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    coins: Optional[List[str]] = Query(None),
):
    with engine.begin() as connection:
        query = (
            select(non_mm_trades_cache.c["user"], func.sum(non_mm_trades_cache.c["group_count"]).label("trade_count"))
            .group_by(non_mm_trades_cache.c["user"])
            .order_by(desc("trade_count"))
            .limit(10)
        )
        query = apply_filters(query, non_mm_trades_cache, start_date, end_date, coins)
        result = connection.execute(query)
        table_data = [
            {"name": row["user"], "value": row["trade_count"]} for row in result
        ]
        return {"table_data": table_data}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
