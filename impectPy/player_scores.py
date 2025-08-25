# load packages
import pandas as pd
import requests
from impectPy.helpers import RateLimitedAPI, unnest_mappings_df, process_response as process_resp
from .matches import getMatchesFromHost
from .iterations import getIterationsFromHost

# define the allowed positions
allowed_positions = [
  "GOALKEEPER",
  "LEFT_WINGBACK_DEFENDER",
  "RIGHT_WINGBACK_DEFENDER",
  "CENTRAL_DEFENDER",
  "DEFENSE_MIDFIELD",
  "CENTRAL_MIDFIELD",
  "ATTACKING_MIDFIELD",
  "LEFT_WINGER",
  "RIGHT_WINGER",
  "CENTER_FORWARD"
]

######
#
# This function returns a pandas dataframe that contains all scores for a
# given match and a given set of positions aggregated per player
#
######


def getPlayerMatchScores(
        matches: list, positions: list, token: str, session: requests.Session = requests.Session()
) -> pd.DataFrame:

    # create an instance of RateLimitedAPI
    connection = RateLimitedAPI(session)

    # construct header with access token
    connection.session.headers.update({"Authorization": f"Bearer {token}"})

    return getPlayerMatchScoresFromHost(matches, positions, connection, "https://api.impect.com")

def getPlayerMatchScoresFromHost(matches: list, positions: list, connection: RateLimitedAPI, host: str) -> pd.DataFrame:

    # check input for matches argument
    if not isinstance(matches, list):
        raise Exception("Argument 'matches' must be a list of integers.")

    # check input for positions argument
    if not isinstance(positions, list):
        raise Exception("Input for positions argument must be a list")

    # check if the input positions are valid
    invalid_positions = [position for position in positions if position not in allowed_positions]
    if len(invalid_positions) > 0:
        raise Exception(
            f"Invalid position(s): {', '.join(invalid_positions)}."
            f"\nChoose one or more of: {', '.join(allowed_positions)}"
        )

    # get match info
    iterations = pd.concat(
        map(lambda match: process_resp(
            connection.make_api_request_limited(
                url=f"{host}/v5/customerapi/matches/{match}",
                method="GET"
            ),
            endpoint="Iterations"
        ), matches),
        ignore_index=True
    )

    # filter for matches that are unavailable
    fail_matches = iterations[iterations.lastCalculationDate.isnull()].id.drop_duplicates().to_list()

    # drop matches that are unavailable from list of matches
    matches = [match for match in matches if match not in fail_matches]

    # raise warnings
    if len(fail_matches) > 0:
        if len(matches) == 0:
            raise Exception("All supplied matches are unavailable. Execution stopped.")
        else:
            print(f"The following matches are not available yet and were ignored:\n{fail_matches}")

    # extract iterationIds
    iterations = list(iterations[iterations.lastCalculationDate.notnull()].iterationId.unique())

    # compile list of positions
    position_string = ",".join(positions)

    # get player scores
    scores_raw = pd.concat(
        map(lambda match: process_resp(
            connection.make_api_request_limited(
                url=f"{host}/v5/customerapi/matches/{match}/positions/{position_string}/player-scores",
                method="GET"
            ),
            endpoint="PlayerMatchScores"
        ).assign(
            matchId=match,
            positions=position_string
        ), matches),
        ignore_index=True
    )

    # get players
    players = pd.concat(
        map(
            lambda iteration: process_resp(
                connection.make_api_request_limited(
                    url=f"{host}/v5/customerapi/iterations/{iteration}/players",
                    method="GET"
                ),
                endpoint="Players"
            ),
            iterations
        ),
        ignore_index=True
    )[["id", "commonname", "firstname", "lastname", "birthdate", "birthplace", "leg", "countryIds", "idMappings"]]

    # only keep first country id for each player
    country_series = players["countryIds"].explode().groupby(level=0).first()
    players["countryIds"] = players.index.to_series().map(country_series).astype("float").astype("Int64")
    players = players.rename(columns={"countryIds": "countryId"})

    # unnest mappings
    players = unnest_mappings_df(players, "idMappings").drop(["idMappings"], axis=1).drop_duplicates()

    # get squads
    squads = pd.concat(
        map(lambda iteration: process_resp(
            connection.make_api_request_limited(
                url=f"{host}/v5/customerapi/iterations/{iteration}/squads",
                method="GET"
            ),
            endpoint="Squads"
        ), iterations),
        ignore_index=True
    )[["id", "name"]].drop_duplicates()

    # get player scores
    scores = process_resp(
        connection.make_api_request_limited(
            url=f"{host}/v5/customerapi/player-scores",
            method="GET"
        ),
        endpoint="PlayerScores"
    )[["id", "name"]]

    # get matches
    matchplan = pd.concat(
        map(lambda iteration: getMatchesFromHost(
            iteration=iteration,
            connection=connection,
            host=host
        ),
            iterations),
        ignore_index=True)

    # get iterations
    iterations = getIterationsFromHost(connection=connection, host=host)

    # get country data
    countries = process_resp(
        connection.make_api_request_limited(
            url=f"{host}/v5/customerapi/countries",
            method="GET"
        ),
        endpoint="KPIs"
    )

    # create empty df to store player scores
    player_scores = pd.DataFrame()

    # manipulate player_scores

    # iterate over matches
    for i in range(len(scores_raw)):

        # create empty df to store per match scores
        match_player_scores = pd.DataFrame()

        # iterate over sides
        for side in ["squadHomePlayers", "squadAwayPlayers"]:

            # get data for index
            temp = scores_raw[side].loc[i]

            # check if any records for side at given position
            if len(temp) == 0:
                continue

            # convert to pandas df
            temp = pd.DataFrame(temp).assign(
                matchId=scores_raw.matchId.loc[i],
                squadId=scores_raw[side.replace("Players", "Id")].loc[i],
                positions=scores_raw.positions.loc[i]
            )

            # extract matchshares
            matchshares = temp[["matchId", "squadId", "id", "matchShare", "playDuration"]].drop_duplicates().assign(
                positions=position_string
            )

            # explode kpis column
            temp = temp.explode("playerScores")

            # unnest dictionary in kpis column
            temp = pd.concat(
                [temp.drop(["playerScores"], axis=1), temp["playerScores"].apply(pd.Series)],
                axis=1
            )

            # merge with player scores to ensure all scores are present
            temp = pd.merge(
                temp,
                scores,
                left_on="playerScoreId",
                right_on="id",
                how="outer",
                suffixes=("", "_right")
            )

            # pivot data
            temp = pd.pivot_table(
                temp,
                values="value",
                index=["matchId", "squadId", "positions", "id"],
                columns="name",
                aggfunc="sum",
                fill_value=0,
                dropna=False
            ).reset_index()

            # inner join with matchshares
            temp = pd.merge(
                temp,
                matchshares,
                left_on=["matchId", "squadId", "id", "positions"],
                right_on=["matchId", "squadId", "id", "positions"],
                how="inner",
                suffixes=("", "_right")
            )

            # append to match_player_scores
            match_player_scores = pd.concat([match_player_scores, temp])

        # check if any records for match at given position
        if len(match_player_scores) == 0:
            print(f"No players played at given position in match {scores_raw.loc[i].matchId}")

        # append to player_scores
        player_scores = pd.concat([player_scores, match_player_scores])

    # check if any records for any match at given position
    if len(player_scores) == 0:
            raise Exception("No players played at given positions for any given match. Execution stopped.")

    # merge with other data
    player_scores = player_scores.merge(
        matchplan[["id", "scheduledDate", "matchDayIndex", "matchDayName", "iterationId"]],
        left_on="matchId",
        right_on="id",
        how="left",
        suffixes=("", "_right")
    ).merge(
        iterations[["id", "competitionId", "competitionName", "competitionType", "season"]],
        left_on="iterationId",
        right_on="id",
        how="left",
        suffixes=("", "_right")
    ).merge(
        squads[["id", "name"]].rename(
            columns={"id": "squadId", "name": "squadName"}
        ),
        left_on="squadId",
        right_on="squadId",
        how="left",
        suffixes=("", "_right")
    ).merge(
        players[[
            "id", "wyscoutId", "heimSpielId", "skillCornerId", "commonname",
            "firstname", "lastname", "birthdate", "birthplace", "countryId", "leg"
        ]].rename(
            columns={"commonname": "playerName"}
        ),
        left_on="id",
        right_on="id",
        how="left",
        suffixes=("", "_right")
    ).merge(
        countries.rename(columns={"fifaName": "playerCountry"}),
        left_on="countryId",
        right_on="id",
        how="left",
        suffixes=("", "_right")
    )

    # rename some columns
    player_scores = player_scores.rename(columns={
        "scheduledDate": "dateTime",
        "id": "playerId"
    })

    # define column order
    order = [
        "matchId",
        "dateTime",
        "competitionName",
        "competitionId",
        "competitionType",
        "iterationId",
        "season",
        "matchDayIndex",
        "matchDayName",
        "squadId",
        "squadName",
        "playerId",
        "wyscoutId",
        "heimSpielId",
        "skillCornerId",
        "playerName",
        "firstname",
        "lastname",
        "birthdate",
        "birthplace",
        "playerCountry",
        "leg",
        "positions",
        "matchShare",
        "playDuration",
    ]

    # add kpiNames to order
    order += scores["name"].to_list()

    # select columns
    player_scores = player_scores[order]

    # fix some column types
    player_scores["matchId"] = player_scores["matchId"].astype("Int64")
    player_scores["squadId"] = player_scores["squadId"].astype("Int64")
    player_scores["playerId"] = player_scores["playerId"].astype("Int64")
    player_scores["wyscoutId"] = player_scores["wyscoutId"].astype("Int64")
    player_scores["heimSpielId"] = player_scores["heimSpielId"].astype("Int64")
    player_scores["skillCornerId"] = player_scores["skillCornerId"].astype("Int64")

    # return data
    return player_scores


######
#
# This function returns a pandas dataframe that contains all scores for a
# given iteration and a given set of positions aggregated per player
#
######


def getPlayerIterationScores(
        iteration: int, positions: list, token: str, session: requests.Session = requests.Session()
) -> pd.DataFrame:

    # create an instance of RateLimitedAPI
    connection = RateLimitedAPI(session)

    # construct header with access token
    connection.session.headers.update({"Authorization": f"Bearer {token}"})

    return getPlayerIterationScoresFromHost(iteration, positions, connection, "https://api.impect.com")

def getPlayerIterationScoresFromHost(
        iteration: int, positions: list, connection: RateLimitedAPI, host: str
) -> pd.DataFrame:

    # check input for iteration argument
    if not isinstance(iteration, int):
        raise Exception("Input for iteration argument must be an integer")

    # check input for positions argument
    if not isinstance(positions, list):
        raise Exception("Input for positions argument must be a list")

    # check if the input positions are valid
    invalid_positions = [position for position in positions if position not in allowed_positions]
    if len(invalid_positions) > 0:
        raise Exception(
            f"Invalid position(s): {', '.join(invalid_positions)}."
            f"\nChoose one or more of: {', '.join(allowed_positions)}"
        )

    # get squads
    squads = process_resp(
        connection.make_api_request_limited(
            url=f"{host}/v5/customerapi/iterations/{iteration}/squads",
            method="GET"
        ),
        endpoint="Squads"
    )

    # get squadIds
    squad_ids = squads[squads.access].id.to_list()

    # compile position string
    position_string = ",".join(positions)

    # get player iteration averages per squad
    scores_raw = pd.concat(
        map(lambda squadId: process_resp(
            connection.make_api_request_limited(
                url=f"{host}/v5/customerapi/iterations/{iteration}/"
                    f"squads/{squadId}/positions/{position_string}/player-scores",
                method="GET"
            ),
            endpoint="PlayerIterationScores",
            raise_exception=False
        ).assign(
            iterationId=iteration,
            squadId=squadId,
            positions=position_string
        ), squad_ids),
        ignore_index=True
    )

    # raise exception if no player played at given positions in entire iteration
    if len(scores_raw) == 0:
        raise Exception(f"No players played at given position in iteration {iteration}.")

    # print squads without players at given position
    error_list = [str(squadId) for squadId in squad_ids if squadId not in scores_raw.squadId.to_list()]
    if len(error_list) > 0:
        print(f"No players played at positions {positions} for iteration {iteration} for following squads:\n\t{', '.join(error_list)}")

    # get players
    players = process_resp(
        connection.make_api_request_limited(
            url=f"{host}/v5/customerapi/iterations/{iteration}/players",
            method="GET"
        ),
        endpoint="Players"
    )[["id", "commonname", "firstname", "lastname", "birthdate", "birthplace", "leg", "countryIds", "idMappings"]]

    # only keep first country id for each player
    country_series = players["countryIds"].explode().groupby(level=0).first()
    players["countryIds"] = players.index.to_series().map(country_series).astype("float").astype("Int64")
    players = players.rename(columns={"countryIds": "countryId"})

    # unnest mappings
    players = unnest_mappings_df(players, "idMappings").drop(["idMappings"], axis=1).drop_duplicates()

    # get scores
    scores = process_resp(
        connection.make_api_request_limited(
            url=f"{host}/v5/customerapi/player-scores",
            method="GET"
        ),
        endpoint="playerScores"
    )[["id", "name"]]

    # get iterations
    iterations = getIterationsFromHost(connection=connection, host=host)

    # get country data
    countries = process_resp(
        connection.make_api_request_limited(
            url=f"{host}/v5/customerapi/countries",
            method="GET"
        ),
        endpoint="KPIs"
    )

    # unnest scorings
    averages = scores_raw.explode("playerScores").reset_index(drop=True)

    # unnest dictionary in kpis column
    averages = pd.concat(
        [averages.drop(["playerScores"], axis=1), pd.json_normalize(averages["playerScores"])],
        axis=1
    )

    # merge with player scores to ensure all kpis are present
    averages = averages.merge(
        scores,
        left_on="playerScoreId",
        right_on="id",
        how="outer",
        suffixes=("", "_right")
    )

    # get matchShares
    match_shares = averages[
        ["iterationId", "squadId", "playerId", "positions", "playDuration", "matchShare"]].drop_duplicates()

    # fill missing values in the "name" column with a default value to ensure players without scorings don't get lost
    if len(averages["name"][averages["name"].isnull()]) > 0:
        averages["name"] = averages["name"].fillna("-1")

    # pivot kpi values
    averages = pd.pivot_table(
        averages,
        values="value",
        index=["iterationId", "squadId", "playerId", "positions"],
        columns="name",
        aggfunc="sum",
        fill_value=0,
        dropna=False
    ).reset_index()

    # drop "-1" column
    if "-1" in averages.columns:
        averages.drop(["-1"], inplace=True, axis=1)

    # merge with playDuration and matchShare
    averages = averages.merge(
        match_shares,
        left_on=["iterationId", "squadId", "playerId", "positions"],
        right_on=["iterationId", "squadId", "playerId", "positions"],
        how="inner",
        suffixes=("", "_right")
    )
    # merge with other data
    averages = averages.merge(
        iterations[["id", "competitionName", "season"]],
        left_on="iterationId",
        right_on="id",
        how="left",
        suffixes=("", "_right")
    ).merge(
        squads[["id", "name"]].rename(
            columns={"id": "squadId", "name": "squadName"}
        ),
        left_on="squadId",
        right_on="squadId",
        how="left",
        suffixes=("", "_right")
    ).merge(
        players[[
            "id", "wyscoutId", "heimSpielId", "skillCornerId", "commonname",
            "firstname", "lastname", "birthdate", "birthplace", "countryId", "leg"
        ]].rename(
            columns={"commonname": "playerName"}
        ),
        left_on="playerId",
        right_on="id",
        how="left",
        suffixes=("", "_right")
    ).merge(
        countries.rename(columns={"fifaName": "playerCountry"}),
        left_on="countryId",
        right_on="id",
        how="left",
        suffixes=("", "_right")
    )

    # remove NA rows
    averages = averages[averages.iterationId.notnull()]

    # fix column types
    averages["squadId"] = averages["squadId"].astype(int)
    averages["playerId"] = averages["playerId"].astype(int)
    averages["iterationId"] = averages["iterationId"].astype(int)

    # define column order
    order = [
        "iterationId",
        "competitionName",
        "season",
        "squadId",
        "squadName",
        "playerId",
        "wyscoutId",
        "heimSpielId",
        "skillCornerId",
        "playerName",
        "firstname",
        "lastname",
        "birthdate",
        "birthplace",
        "playerCountry",
        "leg",
        "positions",
        "matchShare",
        "playDuration"
    ]

    # add kpiNames to order
    order = order + scores.name.to_list()

    # select columns
    averages = averages[order]

    # fix some column types
    averages["squadId"] = averages["squadId"].astype("Int64")
    averages["playerId"] = averages["playerId"].astype("Int64")
    averages["wyscoutId"] = averages["wyscoutId"].astype("Int64")
    averages["heimSpielId"] = averages["heimSpielId"].astype("Int64")
    averages["skillCornerId"] = averages["skillCornerId"].astype("Int64")

    # return result
    return averages


######
#
# This function calculates xG/90 (open play, non-penalty) for each player in a single iteration
#
######


def getPlayerOpenPlayXG90(
        iteration: int, positions: list, token: str, session: requests.Session = requests.Session()
) -> pd.DataFrame:
    """
    Calculate xG/90 (open play, non-penalty) for each player in a single iteration.
    
    Args:
        iteration: The iteration ID to get data for
        positions: List of positions to filter players by
        token: API access token
        session: Optional requests session
    
    Returns:
        DataFrame with player data including xG/90 for open play, non-penalty situations
    """

    # create an instance of RateLimitedAPI
    connection = RateLimitedAPI(session)

    # construct header with access token
    connection.session.headers.update({"Authorization": f"Bearer {token}"})

    return getPlayerOpenPlayXG90FromHost(iteration, positions, connection, "https://api.impect.com")


def getPlayerOpenPlayXG90FromHost(
        iteration: int, positions: list, connection: RateLimitedAPI, host: str
) -> pd.DataFrame:
    """
    Calculate xG/90 (open play, non-penalty) for each player in a single iteration.
    
    This function gets events data to filter for open play shots (excluding penalties)
    and calculates the per-90-minute xG rate for each player.
    """
    
    # check input for iteration argument
    if not isinstance(iteration, int):
        raise Exception("Input for iteration argument must be an integer")

    # check input for positions argument
    if not isinstance(positions, list):
        raise Exception("Input for positions argument must be a list")

    # check if the input positions are valid
    invalid_positions = [position for position in positions if position not in allowed_positions]
    if len(invalid_positions) > 0:
        raise Exception(
            f"Invalid position(s): {', '.join(invalid_positions)}."
            f"\nChoose one or more of: {', '.join(allowed_positions)}"
        )

    # get matches for this iteration
    matches_data = getMatchesFromHost(iteration=iteration, connection=connection, host=host)
    match_ids = matches_data['id'].tolist()
    
    if len(match_ids) == 0:
        raise Exception(f"No matches found for iteration {iteration}")

    # get events data for all matches in the iteration
    from .events import getEventsFromHost
    events = getEventsFromHost(
        matches=match_ids, 
        include_kpis=True, 
        include_set_pieces=True, 
        connection=connection, 
        host=host
    )
    
    # Strict open-play shot filter
    # - actionType == 'SHOT'
    # - exclude penalty shootouts (periodId == 5)
    # - exclude penalties during the match (action == 'PENALTY')
    # - exclude any set-piece context using event metadata
    shot_filter = (events['actionType'] == 'SHOT') & (events['periodId'] != 5)

    # Exclude in-game penalties (if present)
    if 'action' in events.columns:
        shot_filter &= (events['action'] != 'PENALTY')

    # Exclude inferred set pieces (bool), if present
    if 'inferredSetPiece' in events.columns:
        shot_filter &= (~events['inferredSetPiece'].fillna(False))

    # Exclude explicit set-piece linkage
    if 'setPieceId' in events.columns:
        shot_filter &= (events['setPieceId'].isna())

    # Exclude known set-piece categories (e.g., corner, free kick)
    if 'setPieceCategory' in events.columns:
        shot_filter &= (events['setPieceCategory'].isna())
    if 'adjSetPieceCategory' in events.columns:
        shot_filter &= (events['adjSetPieceCategory'].isna())
    
    open_play_shots = events[shot_filter].copy()
    
    # if no open play shots found, return empty result with proper structure
    if len(open_play_shots) == 0:
        print(f"No open play shots found for iteration {iteration}")
        # get basic player data structure from regular iteration scores
        base_data = getPlayerIterationScoresFromHost(iteration, positions, connection, host)
        base_data['openPlayXG'] = 0.0
        base_data['openPlayXG90'] = 0.0
        return base_data[['iterationId', 'competitionName', 'season', 'squadId', 'squadName', 
                         'playerId', 'playerName', 'positions', 'matchShare', 'playDuration', 
                         'openPlayXG', 'openPlayXG90']]
    
    # aggregate open play xG by player
    player_open_play_xg = open_play_shots.groupby([
        'iterationId', 'squadId', 'playerId', 'playerPosition'
    ]).agg({
        'SHOT_XG': 'sum'
    }).reset_index()
    
    # rename columns for consistency
    player_open_play_xg = player_open_play_xg.rename(columns={
        'SHOT_XG': 'openPlayXG',
        'playerPosition': 'positions'
    })
    
    # get base player iteration data to get match shares and play duration
    base_data = getPlayerIterationScoresFromHost(iteration, positions, connection, host)
    
    # merge open play xG data with base player data
    result = base_data.merge(
        player_open_play_xg,
        on=['iterationId', 'squadId', 'playerId', 'positions'],
        how='left'
    )
    
    # fill missing xG values with 0 (players who didn't take open play shots)
    result['openPlayXG'] = result['openPlayXG'].fillna(0.0)
    
    # calculate xG per 90 minutes
    # playDuration is provided in seconds, so use: xG * (90*60) / playDuration
    result['openPlayXG90'] = result.apply(
        lambda row: (row['openPlayXG'] * 5400 / row['playDuration']) if row['playDuration'] > 0 else 0.0,
        axis=1
    )
    
    # select relevant columns for the result
    result_columns = [
        'iterationId', 'competitionName', 'season', 'squadId', 'squadName',
        'playerId', 'wyscoutId', 'heimSpielId', 'skillCornerId', 'playerName',
        'firstname', 'lastname', 'birthdate', 'birthplace', 'playerCountry', 'leg',
        'positions', 'matchShare', 'playDuration', 'openPlayXG', 'openPlayXG90'
    ]
    
    # return only the columns that exist in the result
    available_columns = [col for col in result_columns if col in result.columns]
    
    return result[available_columns].sort_values(['squadId', 'playerId'])