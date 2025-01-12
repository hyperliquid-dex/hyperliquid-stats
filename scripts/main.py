import datetime
import json
import os

import boto3
import lz4.frame
import pandas as pd
import requests
from sqlalchemy import create_engine
from sqlalchemy.sql import text
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

path = os.path.dirname(os.path.abspath(__file__))
slack_alerts_channel = "#hyperliquid-alerts"

table_to_file_name_map = {
    "non_mm_trades": "non_mm_trades",
    "non_mm_ledger_updates": "ledger_updates",
    "liquidations": "liquidations",
    "funding": "funding",
    "account_values": "account_values",
    "asset_ctxs": "asset_ctxs",
    "market_data": "market_data",
    "total_accrued_fees": "total_accrued_fees",
    "hlp_positions": "hlp_positions",
}

# Load configuration from JSON file
with open("/app/config.json", "r") as config_file:
    config = json.load(config_file)


def get_asset_coin_map() -> dict[int, str]:
    asset_coin_map = {}

    url = "https://api.hyperliquid.xyz/info"
    headers = {
        "Content-Type": "application/json",
    }
    data = {"type": "meta"}
    response = requests.post(url, headers=headers, data=json.dumps(data))
    for i, coin in enumerate(response.json()["universe"]):
        asset_coin_map[i] = coin["name"]

    return asset_coin_map


def download_data_from_s3(bucket_name: str, file_name: str):
    aws_access_key_id = config["aws_access_key_id"]
    aws_secret_access_key = config["aws_secret_access_key"]
    session = boto3.Session(
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )
    s3 = session.resource("s3")
    my_bucket = s3.Bucket(bucket_name)

    # Construct the local file path, ensuring the /tmp directory exists
    local_file_path = os.path.join("../tmp", file_name)
    os.makedirs(os.path.dirname(local_file_path), exist_ok=True)

    my_bucket.download_file(file_name, local_file_path)


def load_data_to_db(db_uri: str, table_name: str, file_name: str, date: datetime.date):
    if "market_data" in file_name:
        return

    with lz4.frame.open(f"../tmp/{file_name}", "r") as f:
        df = pd.read_csv(f)

    update_db_table(db_uri, table_name, df, date)


def update_db_table(db_uri: str, table_name: str, df: pd.DataFrame, date):
    engine = create_engine(db_uri)
    # Clear subset of cache table for that date. This is called once per date
    # per cache table
    if "cache" in table_name and "market_data" not in table_name:
        fmt_date = date.strftime("%Y-%m-%d")
        try:
            engine.execute(f"DELETE FROM {table_name} where time = '{fmt_date}'")
        except Exception as e:
            # table doesn't exist yet
            pass

    try:
        df.to_sql(table_name, con=engine, if_exists="append")
    except:
        data = pd.read_sql(f"SELECT * FROM {table_name}", con=engine)
        data = pd.concat([data, df], ignore_index=True)
        if 'level_0' in data.columns:
            data = data.drop('level_0', axis=1)
        data.to_sql(table_name, con=engine, if_exists="replace")


def get_latest_date(db_uri: str, table_name: str) -> datetime.datetime:
    engine = create_engine(db_uri)
    with engine.connect() as connection:
        try:
            result = connection.execute(text(f"SELECT max(time) FROM {table_name}"))
            return result.scalar()
        except:
            return None


def send_alert(message: str):
    print("Alert:", message)
    slack_token = config["slack_token"]
    if slack_token != "":
        client = WebClient(token=slack_token)

        try:
            response = client.chat_postMessage(
                channel=slack_alerts_channel, text=message
            )
        except SlackApiError as e:
            print(f"Error sending alert: {e.response['error']}")


def generate_dates(start_date: datetime.date):
    end_date = datetime.date.today()
    date_list = [
        start_date + datetime.timedelta(days=x)
        for x in range((end_date - start_date).days + 1)
    ]
    return date_list


def calculate_slippage(row: dict, ntl_value: int):
    # Get the ask levels
    ask_levels = json.loads(row['levels'])[1]

    # Calculate the total liquidity needed to fulfill the ntl value
    total_liquidity_needed = ntl_value

    # Calculate the average executed price and slippage
    average_executed_price = 0
    filled_liquidity = 0
    for level in ask_levels:
        liquidity = float(level['px']) * float(level['sz'])
        price = float(level['px'])
        if filled_liquidity + liquidity >= total_liquidity_needed:
            # Calculate the remaining liquidity needed to fulfill the ntl value
            remaining_liquidity = total_liquidity_needed - filled_liquidity

            # Calculate the liquidity percentage filled at the current level
            liquidity_percentage_filled = remaining_liquidity / total_liquidity_needed

            # Calculate the average executed price based on the liquidity percentage filled
            average_executed_price += liquidity_percentage_filled * price

            filled_liquidity = total_liquidity_needed
            break
        else:
            # Add the current level's liquidity to the filled liquidity
            filled_liquidity += liquidity

            # Calculate the liquidity percentage filled at the current level
            liquidity_percentage_filled = liquidity / total_liquidity_needed

            # Calculate the average executed price based on the liquidity percentage filled
            average_executed_price += liquidity_percentage_filled * price

    # Calculate the slippage
    if filled_liquidity >= total_liquidity_needed:
        slippage = abs(average_executed_price / row['mid'] - 1)
    else:
        slippage = 1

    return slippage


def update_market_data_cache(db_uri: str, date: datetime.date, file_name: str):
    engine = create_engine(db_uri)
    with lz4.frame.open(f"../tmp/{file_name}", "r") as f:
        data = [
            {
                "time": pd.Timestamp(json_line["time"]),
                "ver_num": json_line["ver_num"],
                "channel": json_line["raw"]["channel"],
                "coin": json_line["raw"]["data"]["coin"],
                "raw_time": json_line["raw"]["data"]["time"],
                "liquidity": sum(
                    float(bid_or_ask["px"]) * float(bid_or_ask["sz"])
                    for level in json_line["raw"]["data"]["levels"]
                    for bid_or_ask in level
                ),
                "levels": json.dumps(json_line["raw"]["data"]["levels"])
            }
            for line in f
            for json_line in [json.loads(line)]
        ]
    df = pd.DataFrame(data)
    df["time"] = date

    # Extract highest_bid and lowest_ask from the levels lists
    df['highest_bid'] = df['levels'].apply(lambda levels: float(json.loads(levels)[0][0]['px']))
    df['lowest_ask'] = df['levels'].apply(lambda levels: float(json.loads(levels)[1][0]['px']))

    # Calculate the mid price
    df['mid'] = (df['highest_bid'] + df['lowest_ask']) / 2

    ntl_values = [0.01, 1000, 3000, 10000, 30000, 100000]

    # Calculate the slippage for each ntl value
    for ntl_value in ntl_values:
        slippage_column = f"slippage_{ntl_value}"
        df[slippage_column] = df.apply(lambda row: calculate_slippage(row, ntl_value), axis=1)

    aggregated_df = df.groupby(['time', 'coin']).agg(
        median_liquidity=('liquidity', lambda x: x.median()),
        median_slippage_0=('slippage_0.01', lambda x: x.median()),
        median_slippage_1000=('slippage_1000', lambda x: x.median()),
        median_slippage_3000=('slippage_3000', lambda x: x.median()),
        median_slippage_10000=('slippage_10000', lambda x: x.median()),
        median_slippage_30000=('slippage_30000', lambda x: x.median()),
        median_slippage_100000=('slippage_100000', lambda x: x.median()),
        mid_price=('mid', lambda x: x.mean()),
    )
    aggregated_df = aggregated_df.reset_index()
    update_db_table(db_uri, "market_data_cache", aggregated_df, date)


def generate_hlp_positions(date: datetime.date):
    asset_ctx_fln = f"asset_ctxs/{date.strftime('%Y%m%d')}.csv.lz4"
    hlp_fln = f"hlp_positions/{date.strftime('%Y%m%d')}.csv.lz4"

    with lz4.frame.open(f"../tmp/{asset_ctx_fln}", "r") as f:
        asset_ctx_df = pd.read_csv(f)
    with lz4.frame.open(f"../tmp/{hlp_fln}", "r") as f:
        hlp_df = pd.read_csv(f)

    asset_ctx_df = asset_ctx_df[["time", "coin", "oracle_px"]]
    asset_ctx_df = asset_ctx_df.set_index("time")
    hlp_df = hlp_df.set_index("time")
    df = asset_ctx_df.join(hlp_df, how="right")

    def ntl(row):
        return row[row["coin"]] * row["oracle_px"]

    df["ntl"] = df.apply(lambda row: ntl(row), axis=1)
    df["ntl_abs"] = df["ntl"].abs()

    df = df[["coin", "ntl", "ntl_abs"]]
    df = df.groupby(["coin"]).agg({"ntl": "mean", "ntl_abs": "mean"})
    df = df.reset_index()
    df["time"] = date
    return df


def update_cache_tables(db_uri: str, file_name: str, date: datetime.date):
    # Reads the file saved by s3 of date and cache table with the new data
    if "market_data" in file_name:
        update_market_data_cache(db_uri, date, file_name)
    else:
        engine = create_engine(db_uri)
        with lz4.frame.open(f"../tmp/{file_name}", "r") as f:
            df = pd.read_csv(f)

        if "trades" in file_name:
            if "tif" not in df.columns:
                df["tif"] = "Gtc"
            df["tif"] = df["tif"].fillna("Na")
            df_agg = (
                df.groupby(["user", "coin", "side", "crossed", "special_trade_type", "tif"])
                .agg({"px": "mean", "sz": "sum"})
                .reset_index()
            )

            df_agg.columns = [
                "user",
                "coin",
                "side",
                "crossed",
                "special_trade_type",
                "tif",
                "mean_px",
                "sum_sz",
            ]
            df_agg["group_count"] = df.groupby(
                ["user", "coin", "side", "crossed", "special_trade_type"]
            )["user"].transform("count")
            df_agg["time"] = date
            df_agg["usd_volume"] = df_agg["mean_px"] * df_agg["sum_sz"]
            df_agg["liquidated_volume"] = df_agg["usd_volume"].where(df_agg["tif"] == "LiquidationMarket", 0.0)
            update_db_table(db_uri, "non_mm_trades_cache", df_agg, date)

        elif "ledger_updates" in file_name:
            df_agg = df.groupby(["user"]).agg({"delta_usd": "sum"}).reset_index()
            df_agg.columns = ["user", "sum_delta_usd"]
            df_agg["time"] = date
            update_db_table(db_uri, "non_mm_ledger_updates_cache", df_agg, date)

        elif "liquidations" in file_name:
            df_agg = (
                df.groupby(["user", "leverage_type"])
                .agg({"liquidated_ntl_pos": "sum", "liquidated_account_value": "sum"})
                .reset_index()
            )
            df_agg.columns = [
                "user",
                "leverage_type",
                "sum_liquidated_ntl_pos",
                "sum_liquidated_account_value",
            ]
            df_agg["time"] = date
            update_db_table(db_uri, "liquidations_cache", df_agg, date)

        elif "funding" in file_name:
            df_agg = (
                df.groupby(["coin"])
                .agg({"funding": "sum", "premium": "sum"})
                .reset_index()
            )
            df_agg.columns = ["coin", "sum_funding", "sum_premium"]
            df_agg["time"] = date
            update_db_table(db_uri, "funding_cache", df_agg, date)

        elif "account_values" in file_name:
            df_agg = (
                df.groupby(["user", "is_vault"])
                .agg({"account_value": "last", "cum_vlm": "last", "cum_ledger": "last"})
                .reset_index()
            )
            df_agg.columns = [
                "user",
                "is_vault",
                "last_account_value",
                "last_cum_vlm",
                "last_cum_ledger",
            ]
            df_agg["time"] = date
            update_db_table(db_uri, "account_values_cache", df_agg, date)

        elif "asset_ctxs" in file_name:
            df_agg = (
                df.groupby(["coin"])
                .agg(
                    {
                        "funding": "sum",
                        "open_interest": "mean",
                        "prev_day_px": "mean",
                        "day_ntl_vlm": "last",
                        "premium": "mean",
                        "oracle_px": ["mean", "first", "last"],
                        "mark_px": "mean",
                        "mid_px": "mean",
                        "impact_bid_px": "mean",
                        "impact_ask_px": "mean",
                    }
                )
                .reset_index()
            )
            df_agg.columns = [
                "coin",
                "sum_funding",
                "avg_open_interest",
                "avg_prev_day_px",
                "avg_day_ntl_vlm",
                "avg_premium",
                "avg_oracle_px",
                "first_oracle_px",
                "last_oracle_px",
                "avg_mark_px",
                "avg_mid_px",
                "avg_impact_bid_px",
                "avg_impact_ask_px",
            ]
            df_agg["time"] = date
            update_db_table(db_uri, "asset_ctxs_cache", df_agg, date)

        elif "total_accrued_fees" in file_name:
            df["time"] = date
            update_db_table(db_uri, "total_accrued_fees_cache", df, date)

        elif "hlp_positions" in file_name:
            df = generate_hlp_positions(date)
            update_db_table(db_uri, "hlp_positions_cache", df, date)


def process_file(
    db_uri: str, bucket_name: str, file_name: str, table: str, date: datetime.date
):
    if "hlp_positions" in table:
        asset_ctxs_fln = f"asset_ctxs/{date.strftime('%Y%m%d')}.csv.lz4"
        download_data_from_s3(bucket_name, asset_ctxs_fln)

    download_data_from_s3(bucket_name, file_name)
    load_data_to_db(db_uri, table, file_name, date)
    update_cache_tables(db_uri, file_name, date)
    tmp_file_path = os.path.join("../tmp", file_name)
    if os.path.isfile(tmp_file_path):
        os.remove(tmp_file_path)
        if "hlp_positions" in table:
            os.remove(os.path.join("../tmp", asset_ctxs_fln))
    else:
        raise Exception(f"Error: {tmp_file_path} not found")


def market_data_exists(db_uri, date):
    engine = create_engine(db_uri)
    fmt_date = date.strftime('%Y-%m-%d')
    try:
        df = pd.read_sql(f"SELECT * FROM market_data_cache where time = '{fmt_date}' LIMIT 1", con=engine)
        return len(df) > 0
    except Exception as e:
        return False


def drop_base_table(db_uri, table):
    engine = create_engine(db_uri)
    engine.execute(f"DROP TABLE IF EXISTS {table}")


def main():
    bucket_name = config["bucket_name"]
    db_uri = config["db_uri"]
    tables = config["tables"]
    asset_coin_map = get_asset_coin_map()

    for table in tables:
        table_name = table_to_file_name_map[table]
        latest_date = get_latest_date(db_uri, f'{table}_cache')
        if isinstance(latest_date, datetime.datetime):
            latest_date = latest_date.date()
        elif not latest_date:
            drop_base_table(db_uri, table)
            latest_date = datetime.date.today() - datetime.timedelta(days=85)

        dates = generate_dates(latest_date)
        if not len(dates):
            send_alert(f"Nothing to process for {table} at {latest_date}")
            print(f"Nothing to process for {table} at {latest_date}")
        for date in dates[1:]:
            try:
                if table_name == "market_data":
                    if market_data_exists(db_uri, date):
                        send_alert(f"Market data already exists for {date}")
                        continue
                    for i in range(24):
                        for asset in asset_coin_map.values():
                            try:
                                file_name = f"{table_name}/{date.strftime('%Y%m%d')}/{i}/l2Book/{asset}.lz4"
                                process_file(
                                    db_uri, bucket_name, file_name, table, date
                                )
                                print(
                                    f"Data processing completed successfully for {date, i, asset, table}!"
                                )
                            except Exception as e:
                                print(f"Error processing {date, i, asset, table}!")
                else:
                    file_name = f"{table_name}/{date.strftime('%Y%m%d')}.csv.lz4"
                    process_file(db_uri, bucket_name, file_name, table, date)

                send_alert(f"Data processing completed successfully for {date, table}!")
                print(f"Data processing completed successfully for {date, table}!")
            except Exception as e:
                send_alert(
                    f"Data processing for {table} as {date} failed with error: {e}"
                )
                print(f"Data processing for {table} as {date} failed with error: {e}")

        # Check cache table max date and compare with the main table
        cache_max_date = get_latest_date(db_uri, f"{table}_cache")
        if cache_max_date and str(cache_max_date)[:10] != str(get_latest_date(db_uri, table))[:10]:
            send_alert(
                f"Cache table for {table} has a different max date ({cache_max_date}) than the main table ({get_latest_date(db_uri, table)})"
            )


if __name__ == "__main__":
    main()
