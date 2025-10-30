import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional
from enum import Enum

from edupage_api.exceptions import (
    FailedToChangeMealError,
    FailedToRateException,
    InvalidMealsData,
    NotLoggedInException,
)
from edupage_api.module import EdupageModule, Module, ModuleHelper
import copy


@dataclass
class Rating:
    __date: str
    __boarder_id: str

    quality_average: float
    quality_ratings: float

    quantity_average: float
    quantity_ratings: float

    def rate(self, edupage: EdupageModule, quantity: int, quality: int):
        if not edupage.is_logged_in:
            raise NotLoggedInException()

        request_url = f"https://{edupage.subdomain}.edupage.org/menu/"

        data = {
            "akcia": "ulozHodnotenia",
            "stravnikid": self.__boarder_id,
            "mysqlDate": self.__date,
            "jedlo_dna": "2",
            "kvalita": str(quality),
            "mnozstvo": str(quantity),
        }

        response = edupage.session.post(request_url, data=data)
        parsed_response = json.loads(response.content.decode())

        error = parsed_response.get("error")
        if error is None or error != "":
            raise FailedToRateException()


@dataclass
class Menu:
    name: str
    allergens: str
    weight: str
    number: str
    rating: Optional[Rating]

class MealType(Enum):
    SNACK = 1
    LUNCH = 2
    AFTERNOON_SNACK = 3

@dataclass
class Meal:
    served_from: Optional[datetime]
    served_to: Optional[datetime]
    amount_of_foods: int
    chooseable_menus: list[str]
    can_be_changed_until: datetime
    title: str
    menus: List[Menu]
    date: datetime
    ordered_meal: Optional[str]
    meal_type: MealType
    __boarder_id: str
    __meal_index: str

    def __iter__(self):
        return iter(self.menus)

    def __make_choice(self, edupage: EdupageModule, choice_str: str):
        request_url = f"https://{edupage.subdomain}.edupage.org/menu/"

        boarder_menu = {
            "stravnikid": self.__boarder_id,
            "mysqlDate": self.date.strftime("%Y-%m-%d"),
            "jids": {self.__meal_index: choice_str},
            "view": "pc_listok",
            "pravo": "Student",
        }

        data = {
            "akcia": "ulozJedlaStravnika",
            "jedlaStravnika": json.dumps(boarder_menu),
        }

        response = edupage.session.post(
            request_url, data=data
        ).content.decode()

        if json.loads(response).get("error") != "":
            raise FailedToChangeMealError()

    def choose(self, edupage: EdupageModule, number: int):
        letters = "ABCDEFGH"
        letter = letters[number - 1]

        self.__make_choice(edupage, letter)
        self.ordered_meal = letter

    def sign_off(self, edupage: EdupageModule):
        self.__make_choice(edupage, "AX")
        self.ordered_meal = None

@dataclass
class Meals:
    snack: Optional[Meal]
    lunch: Optional[Meal]
    afternoon_snack: Optional[Meal]
    


class Lunches(Module):
    def parse_meal(self, meal_index: str, meal: dict, boarder_id: str, date: date) -> Optional[Meal]:
        if meal is None:
            return None
        
        if meal.get("isCooking") == False:
            return None

        ordered_meal = None
        meal_record = meal.get("evidencia")

        if meal_record is not None:
            ordered_meal = meal_record.get("stav")

            if ordered_meal == "V":
                ordered_meal = meal_record.get("obj")

        served_from_str = meal.get("vydaj_od")
        served_to_str = meal.get("vydaj_do")

        if served_from_str:
            served_from = datetime.strptime(served_from_str, "%H:%M")
        else:
            served_from = None

        if served_to_str:
            served_to = datetime.strptime(served_to_str, "%H:%M")
        else:
            served_to = None

        title = meal.get("nazov")

        amount_of_foods = meal.get("druhov_jedal")
        chooseable_menus = list(meal.get("choosableMenus").keys())

        can_be_changed_until = meal.get("zmen_do")

        menus = []

        for food in meal.get("rows"):
            if not food:
                continue

            name = food.get("nazov")
            allergens = food.get("alergenyStr")
            weight = food.get("hmotnostiStr")
            number = food.get("menusStr")
            rating = None

            if number is not None:
                number = number.replace(": ", "")
                rating = meal.get("hodnotenia")
                if rating is not None and rating:
                    rating = rating.get(number)

                    [quality, quantity] = rating

                    quality_average = quality.get("priemer")
                    quality_ratings = quality.get("pocet")

                    quantity_average = quantity.get("priemer")
                    quantity_ratings = quantity.get("pocet")

                    rating = Rating(
                        date.strftime("%Y-%m-%d"),
                        boarder_id,
                        quality_average,
                        quantity_average,
                        quality_ratings,
                        quantity_ratings,
                    )
                else:
                    rating = None
            menus.append(Menu(name, allergens, weight, number, rating))
        
        return Meal(
            served_from,
            served_to,
            amount_of_foods,
            chooseable_menus,
            can_be_changed_until,
            title,
            menus,
            date,
            ordered_meal,
            MealType(int(meal_index)),
            boarder_id,
            meal_index
        )

    @ModuleHelper.logged_in
    def get_meals(self, date: date) -> Optional[Meals]:
        date_strftime = date.strftime("%Y%m%d")
        request_url = f"https://{self.edupage.subdomain}.edupage.org/menu/?date={date_strftime}"
        response = self.edupage.session.get(request_url).content.decode()

        lunch_data = json.loads(
            response.split("edupageData: ")[1].split(",\r\n")[0]
        )

        root = lunch_data.get("robotnik", {}).get("novyListok", lunch_data.get("robotnik", lunch_data))

        # compute monday..friday for the given 'date' param
        monday = date - timedelta(days=date.weekday())
        week_keys = [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]

        def _extract_primary_day(obj):
            """Normalize a day's raw object:
               - prefer the content of '2' if available
               - remove 'nevarisa'
               - if not cooking -> {'isCooking': False}
               - produce { isCooking, (isRating?), menus:{'1'|'2': [...]}, reviews:{'1'|'2': {average,amount}} }
            """
            if not isinstance(obj, dict):
                return {"isCooking": False}

            # pick best source: prefer '2', fallback to merging 0/2/4
            if "2" in obj and isinstance(obj["2"], dict):
                merged = copy.deepcopy(obj["2"])
            else:
                merged = {}
                for k in ("2", "0", "4"):
                    if k in obj and isinstance(obj[k], dict):
                        merged.update(copy.deepcopy(obj[k]))

            # remove unwanted flag
            merged.pop("nevarisa", None)

            # if not cooking then keep only that
            if not merged.get("isCooking"):
                return {"isCooking": False}

            out = {}
            out["isCooking"] = True
            # preserve isRating if present (keeps rating flag info)
            if "isRating" in merged:
                out["isRating"] = merged["isRating"]

            # --- menus: build lists for "1" and "2" with only {name,weight:int} ---
            menus_out = {}
            menus = merged.get("menus", {}) or {}
            for mid in ("1", "2"):
                rows = menus.get(mid, {}).get("rows", []) if isinstance(menus.get(mid, {}), dict) else []
                cleaned = []
                for r in rows:
                    name = r.get("nazov")
                    if not name:
                        continue
                    # weight can appear under a few keys; attempt them
                    weight_val = None
                    for wk in ("hmotnostiStr", "hmotnostStr", "hmotnosti", "hmotnost"):
                        if wk in r and r[wk] is not None:
                            try:
                                # sometimes string, sometimes number; be permissive
                                weight_val = int(float(str(r[wk]).strip()))
                            except Exception:
                                weight_val = None
                            break
                    # if weight missing, default to 0 to keep structure predictable
                    cleaned.append({"name": name, "weight": (weight_val if weight_val is not None else 0)})
                menus_out[mid] = cleaned
            out["menus"] = menus_out

            # --- reviews: create keys "1" and "2", average priemer(s) -> float rounded to 2 dec, amount from first item ---
            hodnotenia = merged.get("hodnotenia", {}) or {}
            reviews_out = {}
            for mid in ("1", "2"):
                arr = hodnotenia.get(mid)
                if arr and isinstance(arr, list) and len(arr) > 0:
                    # remove any 'hodnotenie' fields implicitly by ignoring them
                    pr_list = []
                    for it in arr:
                        if it is None:
                            continue
                        pr = it.get("priemer")
                        if pr is None:
                            continue
                        try:
                            pr_list.append(float(pr))
                        except Exception:
                            pass
                    if pr_list:
                        avg = round(sum(pr_list) / len(pr_list), 2)
                    else:
                        avg = -1.0
                    try:
                        amount = int(arr[0].get("pocet", 0))
                    except Exception:
                        amount = 0
                    reviews_out[mid] = {"average": avg, "amount": amount}
                else:
                    # ensure presence if missing per spec
                    reviews_out[mid] = {"average": -1, "amount": 0}
            out["reviews"] = reviews_out

            return out

        # Build final result for Mon-Fri (ensuring missing days inserted)
        result = {}
        for wk in week_keys:
            day_obj = root.get(wk)
            if day_obj is None:
                result[wk] = {"isCooking": False}
            else:
                result[wk] = _extract_primary_day(day_obj)

        # --- info: move addInfo -> info, move info2 contents, rename fields ---
        add_info = root.get("addInfo") or root.get("addinfo") or {}
        if add_info:
            info = {}
            # id from stravnikid (string -> int)
            sid = add_info.get("stravnikid") or (add_info.get("strRow", {}) or {}).get("stravnikid")
            if sid is not None:
                try:
                    info["id"] = int(sid)
                except Exception:
                    info["id"] = None
            # credit
            info["credit"] = add_info.get("kredit") if add_info.get("kredit") is not None else (
                        add_info.get("info2") or {}).get("kredit")
            # days from info2.pocetDni
            info["days"] = (add_info.get("info2") or {}).get("pocetDni")
            # user: map strRow.meno -> name, priezvisko -> surname
            str_row = add_info.get("strRow") or {}
            if str_row:
                user = {}
                if "meno" in str_row:
                    user["name"] = str_row.get("meno")
                if "priezvisko" in str_row:
                    user["surname"] = str_row.get("priezvisko")
                if user:
                    info["user"] = user

            result["info"] = info

        # replace lunch_data with the cleaned payload
        lunch_data = result

        return result
        
        