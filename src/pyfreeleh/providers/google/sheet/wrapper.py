import json
from typing import Any, Dict, List, Union

import requests
from googleapiclient.discovery import build

from pyfreeleh.providers.google.auth.base import GoogleAuthClient

from .base import A1Range, BatchUpdateRowsRequest, InsertRowsResult, UpdateRowsResult


class GoogleSheetWrapper:
    APPEND_MODE_OVERWRITE = "OVERWRITE"
    APPEND_MODE_INSERT = "INSERT_ROWS"
    MAJOR_DIMENSION_ROWS = "ROWS"

    def __init__(self, auth_client: GoogleAuthClient):
        service = build("sheets", "v4", credentials=auth_client.credentials())
        self._auth_client = auth_client
        self._svc = service.spreadsheets()

    def create_spreadsheet(self, title: str) -> str:
        resp = self._svc.create(body={"properties": {"title": title}}).execute()
        return str(resp["spreadsheetId"])

    def create_sheet(self, spreadsheet_id: str, sheet_name: str) -> str:
        resp = self._svc.batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": {"addSheet": {"properties": {"title": sheet_name}}}}
        ).execute()
        return str(resp["replies"][0]["addSheet"]["properties"]["sheetId"])

    def delete_sheet(self, spreadsheet_id: str, sheet_id: str) -> None:
        self._svc.batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": {"deleteSheet": {"sheetId": sheet_id}}}
        ).execute()

    def insert_rows(self, spreadsheet_id: str, range: A1Range, values: List[List[Any]]) -> InsertRowsResult:
        return self._insert_rows(spreadsheet_id, range, values, self.APPEND_MODE_INSERT)

    def overwrite_rows(self, spreadsheet_id: str, range: A1Range, values: List[List[Any]]) -> InsertRowsResult:
        return self._insert_rows(spreadsheet_id, range, values, self.APPEND_MODE_OVERWRITE)

    def _insert_rows(
        self, spreadsheet_id: str, a1_range: A1Range, values: List[List[Any]], mode: str
    ) -> InsertRowsResult:
        resp = (
            self._svc.values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=str(a1_range),
                insertDataOption=mode,
                includeValuesInResponse="true",
                responseValueRenderOption="FORMATTED_VALUE",
                valueInputOption="USER_ENTERED",
                body={"values": values},
            )
            .execute()
        )

        return InsertRowsResult(
            updated_range=A1Range.from_notation(resp["updates"]["updatedData"]["range"]),
            updated_rows=resp["updates"]["updatedRows"],
            updated_columns=resp["updates"]["updatedColumns"],
            updated_cells=resp["updates"]["updatedCells"],
            inserted_values=resp["updates"]["updatedData"]["values"],
        )

    def clear(self, spreadsheet_id: str, ranges: List[A1Range]) -> None:
        self._svc.values().batchClear(spreadsheetId=spreadsheet_id, body={"ranges": [str(r) for r in ranges]}).execute()

    def update_rows(self, spreadsheet_id: str, a1_range: A1Range, values: List[List[Any]]) -> UpdateRowsResult:
        resp = (
            self._svc.values().update(
                spreadsheetId=spreadsheet_id,
                range=str(a1_range),
                includeValuesInResponse="true",
                responseValueRenderOption="FORMATTED_VALUE",
                valueInputOption="USER_ENTERED",
                body={"majorDimension": self.MAJOR_DIMENSION_ROWS, "range": str(a1_range), "values": values},
            )
        ).execute()

        return UpdateRowsResult(
            updated_range=A1Range.from_notation(resp["updatedRange"]),
            updated_rows=resp["updatedRows"],
            updated_columns=resp["updatedColumns"],
            updated_cells=resp["updatedCells"],
            updated_values=resp["updatedData"].get("values", []),
        )

    def batch_update_rows(self, spreadsheet_id: str, requests: List[BatchUpdateRowsRequest]) -> List[UpdateRowsResult]:
        resp = (
            self._svc.values()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "includeValuesInResponse": True,
                    "responseValueRenderOption": "FORMATTED_VALUE",
                    "valueInputOption": "USER_ENTERED",
                    "data": [
                        {
                            "majorDimension": self.MAJOR_DIMENSION_ROWS,
                            "range": str(req.range),
                            "values": req.values,
                        }
                        for req in requests
                    ],
                },
            )
            .execute()
        )

        results = []
        for response in resp["responses"]:
            results.append(
                UpdateRowsResult(
                    updated_range=A1Range.from_notation(response["updatedRange"]),
                    updated_rows=response["updatedRows"],
                    updated_columns=response["updatedColumns"],
                    updated_cells=response["updatedCells"],
                    updated_values=response["updatedData"].get("values", []),
                )
            )

        return results

    def query(self, spreadsheet_id: str, sheet_name: str, query: str, has_header: bool = True) -> List[Dict[str, str]]:
        auth_token = "Bearer " + self._auth_client.credentials().token
        headers = {"contentType": "application/json", "Authorization": auth_token}

        params: Dict[str, Union[str, int]] = {
            "sheet": sheet_name,
            "tqx": "responseHandler:freeleh",
            "tq": query,
            "headers": 1 if has_header else 0,
        }

        url = "https://docs.google.com/spreadsheets/d/{}/gviz/tq".format(spreadsheet_id)
        r = requests.get(url=url, params=params, headers=headers)
        r.raise_for_status()
        return self._convert_query_result(r.text)

    def _convert_query_result(self, response: str) -> List[Dict[str, Any]]:
        # Remove the schema header -> freeleh({...}).
        # We only care about the JSON inside of the bracket.
        start, end = response.index("{"), response.rindex("}")
        resp = json.loads(response[start : end + 1])
        cols = resp["table"]["cols"]
        rows = resp["table"]["rows"]

        results = []
        for row in rows:
            result_row = {}
            for cell_idx, cell in enumerate(row["c"]):
                if not cell:
                    continue

                col = cols[cell_idx]
                result_row[col["id"]] = self._parse_cell(cell, col)
            results.append(result_row)
        return results

    def _parse_cell(self, cell: Dict[str, str], col: Dict[str, str]) -> Any:
        # We might get null if the current cell is empty.
        if not cell or cell["v"] is None:
            return None

        typ = col["type"]
        if typ == "boolean":
            return cell["v"]
        elif typ == "number":
            if "." in cell["f"]:
                return float(cell["f"])

            return int(cell["f"])
        elif typ == "string":
            return cell["v"]
        elif typ in ["date", "datetime", "timeofday"]:
            # return datetime.strptime(cell["f"], col["pattern"])
            return cell["f"]

        raise ValueError("cell type {} is not supported".format(typ))


class GoogleSheetSession:
    def __init__(self, wrapper: GoogleSheetWrapper, spreadsheet_id: str, sheet_name: str) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name

        self._wrapper = wrapper
        self._ensure()

    def _ensure(self):
        try:
            self._wrapper.create_sheet(self.spreadsheet_id, self._scratchpad_name)
        except Exception:
            pass

    def insert_rows(self, a1_range: A1Range, values: List[List[Any]]) -> InsertRowsResult:
        return self._wrapper.insert_rows(self.spreadsheet_id, a1_range, values)

    def overwrite_rows(self, a1_range: A1Range, values: List[List[Any]]) -> InsertRowsResult:
        return self._wrapper.overwrite_rows(self.spreadsheet_id, a1_range, values)

    def clear(self, a1_ranges: List[A1Range]) -> None:
        return self._wrapper.clear(self.spreadsheet_id, a1_ranges)

    def update_rows(self, a1_range: A1Range, values: List[List[Any]]) -> UpdateRowsResult:
        return self._wrapper.update_rows(self.spreadsheet_id, a1_range, values)

    def batch_update_rows(self, requests: List[BatchUpdateRowsRequest]) -> List[UpdateRowsResult]:
        return self._wrapper.batch_update_rows(self.spreadsheet_id, requests)

    def query(self, query: str, has_header: bool = True) -> List[Dict[str, str]]:
        return self._wrapper.query(self.spreadsheet_id, self.sheet_name, query, has_header)


class GoogleSheetScratchpad:
    SCRATCHPAD_BOOKED_VALUE = "BOOKED"

    def __init__(self, session: GoogleSheetSession) -> None:
        self._session = session
        self._scratchpad_cell = self._book_scratchpad_cell()

    def _book_scratchpad_cell(self) -> A1Range:
        a1_range = A1Range.from_notation(self._session.sheet_name)
        result = self._session.overwrite_rows(a1_range, [[self.SCRATCHPAD_BOOKED_VALUE]])
        self._scratchpad_cell = result.updated_range

    def execute(self, formula: str) -> Any:
        result = self._wrapper.update_rows(self._spreadsheet_id, self._scratchpad_cell, [[formula]])
        return result.updated_values[0][0]

    def close(self):
        self._wrapper.clear(self._spreadsheet_id, [self._scratchpad_cell])
